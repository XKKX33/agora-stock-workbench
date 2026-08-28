import { ANALYST_ROLES, DEBATE_ROUNDS, FINAL_PICK_ROLE, validateAnalyst, validateJudgmentRequest, validateJudgmentResult, type Analyst, type AnalystRole, type JudgmentResult } from "./contracts.js";
import type { RoleProvider } from "./provider.js";

export type PublicEvent = { source_seq: number; type: string; data?: Record<string, unknown> };
export type EventSink = (event: Omit<PublicEvent, "source_seq">) => void;

function parseJsonEnvelope(text: string): Record<string, any> | null {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/i, "").trim();
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch { /* 继续尝试从说明文字中提取唯一 JSON 对象 */ }
  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(trimmed.slice(start, end + 1));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch { return null; }
}

/** 模型正常说完话但没套 JSON 外壳时，原文放在这个键下带回给调用方自己判断能不能用。
 *  下划线前缀表示它不是模型返回的字段，而是本层加的旁路信息。 */
const RAW_TEXT = "_text";

// 风控定稿只能给方向。允许"中性"等于允许它交白卷，而这份名单要拿去和规则组比收益——
// 一份全是"中性"的名单比不出任何东西。
const VERDICTS: readonly string[] = ["看多", "看空"];

type DeepItem = JudgmentResult["deep"][number];
type FinalItem = JudgmentResult["final"][number];
type TranscriptEntry = { role: string; round_no: number; content: string };

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string" && entry.trim() !== "") : [];
}

/** 每只股票的分析和辩论互不依赖，串行跑纯属浪费。
 *
 * 实测数据:20 只串行辩论要 115 分钟(平均 5.7 分钟/只),加上分析师阶段共约 2.5 小时。
 * 瓶颈全在等模型返回,本机 CPU 闲着。并发 4 路把墙钟时间压到约 1/4。
 *
 * 为什么要有界而不是 Promise.all 全放出去:上游按并发和 token 限速,20 路齐发会撞
 * 429,而 429 在本工作流里表现为断流(finishReason === "error"),等于自己制造了要
 * 靠退避去救的失败。4 路是留了余量的保守值,可用 PI_AGENT_CONCURRENCY 调。
 *
 * 结果按输入下标回填,所以输出顺序和串行版完全一致——排序和取前 N 的结果不受并发影响。
 */
const DEFAULT_CONCURRENCY = 4;

function resolveConcurrency(): number {
  const raw = Number(process.env.PI_AGENT_CONCURRENCY ?? DEFAULT_CONCURRENCY);
  // 非法值(空串、NaN、0、负数)一律回到默认值:并发数是性能旋钮,不该因为环境变量
  // 写错就让整条链跑不起来。上限 16 防手滑写成 200 把上游打挂。
  if (!Number.isFinite(raw) || raw < 1) return DEFAULT_CONCURRENCY;
  return Math.min(Math.floor(raw), 16);
}

/** 有界并发 map:worker 抢下标，异常按下标存下不打断其他 worker。 */
async function mapWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>
): Promise<Array<{ ok: true; value: R } | { ok: false; error: unknown }>> {
  const results = new Array<{ ok: true; value: R } | { ok: false; error: unknown }>(items.length);
  let cursor = 0;
  const worker = async (): Promise<void> => {
    for (;;) {
      const index = cursor++;
      if (index >= items.length) return;
      try {
        results[index] = { ok: true, value: await fn(items[index]!, index) };
      } catch (error) {
        results[index] = { ok: false, error };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

/** 辩论论点是给人读的文本，不是要被程序按字段消费的结构。
 *
 * 模型讲清了道理却没套 JSON 外壳时，整段原文就是论点——把这只股票整个丢掉是削足适履，
 * 让它们辩起来才是目的。但一句话都没说时绝不编占位文本，那是造假。
 */
function debaterArgument(parsed: Record<string, any>, role: string): { argument: string; citations: string[] } {
  const argument = typeof parsed.argument === "string" ? parsed.argument.trim() : "";
  if (argument) return { argument, citations: stringList(parsed.citations) };
  const raw = typeof parsed[RAW_TEXT] === "string" ? parsed[RAW_TEXT].trim() : "";
  if (raw) return { argument: raw, citations: [] };
  throw new Error(`${role} returned no argument`);
}

interface RiskVerdict { verdict: string; score: number; thesis: string; risks: string[]; action: string; citations: string[] }

function riskChairVerdict(parsed: Record<string, any>): RiskVerdict {
  const verdict = typeof parsed.verdict === "string" ? parsed.verdict.trim() : "";
  // 把模型实际给的值带进错误信息:排障时要能一眼看出它到底回了什么，
  // 光说"invalid"得再去翻日志。
  if (!VERDICTS.includes(verdict)) throw new Error(`risk_chair returned invalid verdict: ${verdict || "(空)"}`);
  const thesis = typeof parsed.thesis === "string" ? parsed.thesis.trim() : "";
  if (!thesis) throw new Error("risk_chair returned no thesis");
  const action = typeof parsed.action === "string" ? parsed.action.trim() : "";
  if (!action) throw new Error("risk_chair returned no action");
  // 评分决定谁进前三，缺了就没有排序依据。原先缺失时回落到规则分，那等于让规则分
  // 冒充辩论结论——名单看着正常，实际根本不是辩出来的。没有就失败。
  if (typeof parsed.score !== "number" || !Number.isFinite(parsed.score) || parsed.score < 0 || parsed.score > 100) {
    throw new Error("risk_chair returned invalid score");
  }
  return { verdict, score: parsed.score, thesis, risks: stringList(parsed.risks), action, citations: stringList(parsed.citations) };
}

function riskControlText(risk: RiskVerdict): string {
  return risk.risks.length ? `${risk.action}；风险:${risk.risks.join("、")}` : risk.action;
}

interface WorkflowContext {
  call: (role: string, payload: Record<string, unknown>) => Promise<Record<string, any>>;
  emit: EventSink;
  inputHash: string;
}

async function runAnalysts(item: { ts_code: string; score: number }, snapshot: Record<string, unknown>, ctx: WorkflowContext): Promise<Record<AnalystRole, Analyst>> {
  const analysts = {} as Record<AnalystRole, Analyst>;
  for (let index = 0; index < ANALYST_ROLES.length; index += 1) {
    const role = ANALYST_ROLES[index];
    const analyst = validateAnalyst(await ctx.call(role, { ...snapshot, score: item.score }), role);
    analysts[role] = analyst;
    ctx.emit({ type: "message.completed", data: { role, stage: "analysis", round_no: index + 1, ts_code: item.ts_code, summary: analyst.conclusion, stance: analyst.stance, risks: analyst.risks, input_hash: ctx.inputHash } });
  }
  return analysts;
}

/** 四轮公开辩论 bull → bear → bull_counter → risk_chair，每轮拿到累积 transcript。 */
async function runPublicDebate(item: DeepItem, snapshot: Record<string, unknown>, ctx: WorkflowContext): Promise<Omit<FinalItem, "rank">> {
  const transcript: TranscriptEntry[] = ANALYST_ROLES.map((role, index) => ({ role, round_no: index + 1, content: item.analysts[role].conclusion }));
  const cases: Record<string, string> = {};
  let risk: RiskVerdict | null = null;
  for (let index = 0; index < DEBATE_ROUNDS.length; index += 1) {
    const role = DEBATE_ROUNDS[index];
    const roundNo = ANALYST_ROLES.length + index + 1;
    const parsed = await ctx.call(role, { ts_code: item.ts_code, score: item.score, snapshot, transcript });
    if (role === "risk_chair") {
      risk = riskChairVerdict(parsed);
      transcript.push({ role, round_no: roundNo, content: risk.thesis });
      ctx.emit({ type: "message.completed", data: { role, stage: "debate", round_no: roundNo, ts_code: item.ts_code, summary: risk.thesis, verdict: risk.verdict, action: risk.action, risks: risk.risks, citations: risk.citations, input_hash: ctx.inputHash } });
      continue;
    }
    const { argument, citations } = debaterArgument(parsed, role);
    cases[role] = argument;
    transcript.push({ role, round_no: roundNo, content: argument });
    ctx.emit({ type: "message.completed", data: { role, stage: "debate", round_no: roundNo, ts_code: item.ts_code, summary: argument, citations, input_hash: ctx.inputHash } });
  }
  if (!risk) throw new Error("risk_chair produced no verdict");
  return {
    ts_code: item.ts_code,
    decision: risk.verdict,
    score: risk.score,
    reason: risk.thesis,
    bull_case: cases.bull,
    bear_case: cases.bear,
    rebuttal: cases.bull_counter,
    risk_control: riskControlText(risk),
  };
}

export async function runWorkflow(runId: string, rawRequest: unknown, provider: RoleProvider, emit: EventSink, signal: AbortSignal, retryBackoffMs?: (attempt: number) => number): Promise<JudgmentResult> {
  const request = validateJudgmentRequest(rawRequest);
  const usage = { input_tokens: 0, output_tokens: 0 };
  // 方法论正文与角色职责由 Python 随请求下发，避免 TS 里再抄一份而两处静默走偏。
  const briefFor = (role: string): string => `${request.methodology.role_briefs[role] ?? ""}\n${request.methodology.text}`.trim();
  // 上游长流式响应会中途断流：stopReason 变成 "error"、usage 归零、正文停在半句话。
  // 断流是网络瞬时故障而不是模型的合法回答，重试同一角色即可恢复；不重试的话整只
  // 股票会在四轮辩论第一轮就被吞掉，final 恒为空。
  //
  // 但"模型正常说完了话、只是没套 JSON 外壳"是另一回事：重试三次大概率还是同样的
  // 文本，白烧三倍 token。这种情况把原文放在 RAW_TEXT 里带回去，让调用方自己决定能不能
  // 用——辩论论点是给人读的文本，能用；风控结论要进台账参与收益对比，不能用。
  const MAX_ATTEMPTS = 3;
  // 断流常常是上游瞬时过载。立刻重试等于往还没缓过来的服务上再捅一刀，实测连打三次
  // 三次全断。指数退避（2s、4s）给它喘息的时间，也是重试真能救回来的前提。
  const backoffMs = retryBackoffMs ?? ((attempt: number) => 2000 * 2 ** (attempt - 1));
  const call = async (role: string, payload: Record<string, unknown>): Promise<Record<string, any>> => {
    const body = JSON.stringify(payload);
    let lastError: Error | null = null;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
      const response = await provider.complete(role, body, signal, briefFor(role), request.model);
      // "length" 是输出配额耗尽，重试也是同样结果，直接失败；
      // "error" 是断流，可重试。
      if (response.finishReason === "length") throw new Error(`${role} returned truncated output (max_tokens)`);
      const text = response.text.trim();
      const parsed = text ? parseJsonEnvelope(response.text) : null;
      if (parsed) {
        usage.input_tokens += response.inputTokens ?? 0;
        usage.output_tokens += response.outputTokens ?? 0;
        return parsed;
      }
      if (text && response.finishReason !== "error") {
        usage.input_tokens += response.inputTokens ?? 0;
        usage.output_tokens += response.outputTokens ?? 0;
        return { [RAW_TEXT]: text };
      }
      lastError = new Error(
        response.finishReason === "error"
          ? `${role} stream aborted upstream (attempt ${attempt}/${MAX_ATTEMPTS})`
          : `${role} returned no output (attempt ${attempt}/${MAX_ATTEMPTS})`
      );
      if (signal.aborted) throw lastError;
      // 最后一次尝试后不必再等，直接把错误抛出去。
      if (attempt < MAX_ATTEMPTS) {
        const wait = backoffMs(attempt);
        if (wait > 0) await new Promise<void>((resolve) => setTimeout(resolve, wait));
      }
    }
    throw lastError ?? new Error(`${role} returned no usable output`);
  };
  const ctx: WorkflowContext = { call, emit, inputHash: request.input_hash };
  emit({ type: "run.started", data: { run_id: runId, input_hash: request.input_hash } });
  const coarse = request.candidates.map((candidate, index) => ({ ts_code: candidate.ts_code, rank: index + 1, score: Number(candidate.score ?? 50), reason: "方法论候选" }));
  for (const item of coarse) if (!Number.isFinite(item.score) || item.score < 0 || item.score > 100) throw new Error("coarse returned invalid score");
  const snapshotByCode = new Map<string, Record<string, unknown>>(request.snapshots.map((snapshot) => [snapshot.ts_code, snapshot]));
  // 单只股票失败是它自己的运气(快照缺失、模型这次没说清),记 message.failed 继续跑。
  // 但取消是外部指令、"每一只都失败"是系统性故障(配额耗尽、上游挂了、断流),这两种
  // 必须抛出去:调用方要能分辨"跑完了但选不出"和"根本没跑起来"。
  const abortIfCancelled = (): void => {
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
  };
  const concurrency = resolveConcurrency();
  const analystErrors: string[] = [];
  // 先剔掉没快照的:它们不消耗模型调用,不该占并发槽位。
  const analystTargets = coarse.filter((item) => {
    if (snapshotByCode.has(item.ts_code)) return true;
    analystErrors.push(`${item.ts_code}: snapshot missing`);
    emit({ type: "message.failed", data: { ts_code: item.ts_code, error: "snapshot missing", input_hash: request.input_hash } });
    return false;
  });
  abortIfCancelled();
  // guard 放在 worker 拿到任务的那一刻:取消后剩余槽位立刻空转退出,不会再发起新调用。
  const analystResults = await mapWithConcurrency(analystTargets, concurrency, async (item) => {
    abortIfCancelled();
    return runAnalysts(item, snapshotByCode.get(item.ts_code)!, ctx);
  });
  const deep: JudgmentResult["deep"] = [];
  for (const [index, outcome] of analystResults.entries()) {
    const item = analystTargets[index]!;
    if (outcome.ok) {
      deep.push({ ts_code: item.ts_code, rank: deep.length + 1, score: item.score, analysts: outcome.value });
      continue;
    }
    // 取消是外部指令,一票否决:不能把"用户点了停止"记成 20 条个股失败。
    if (outcome.error instanceof DOMException && outcome.error.name === "AbortError") throw outcome.error;
    const message = outcome.error instanceof Error ? outcome.error.message : String(outcome.error);
    analystErrors.push(`${item.ts_code}: ${message}`);
    emit({ type: "message.failed", data: { ts_code: item.ts_code, error: message, input_hash: request.input_hash } });
  }
  // 前三名由辩论产生,不再让模型先排一次序。
  //
  // 原先这里有个 debate 角色,职责是把 deep 排成名单、挑前 N 名进辩论。那是**第二次
  // 排序**——规则方法论已经从全市场排到 Top20 了。而它恰好是整条链最脆的一环:一次
  // 结构化输出失败(实测 "selected must be an array"),后面四轮辩论压根不会开始,
  // 前面 60 次分析师调用全部作废。删掉它,20 只全部参辩,谁进前三由风控评分说话。
  abortIfCancelled();
  const debateResults = await mapWithConcurrency(deep, concurrency, async (item) => {
    abortIfCancelled();
    return runPublicDebate(item, snapshotByCode.get(item.ts_code) ?? { ts_code: item.ts_code }, ctx);
  });
  const scored: Array<{ verdict: Omit<FinalItem, "rank">; score: number }> = [];
  const debateErrors: string[] = [];
  for (const [index, outcome] of debateResults.entries()) {
    const item = deep[index]!;
    if (outcome.ok) {
      scored.push({ verdict: outcome.value, score: outcome.value.score });
      continue;
    }
    if (outcome.error instanceof DOMException && outcome.error.name === "AbortError") throw outcome.error;
    const message = outcome.error instanceof Error ? outcome.error.message : String(outcome.error);
    debateErrors.push(`${item.ts_code}: ${message}`);
    emit({ type: "message.failed", data: { ts_code: item.ts_code, error: message, input_hash: request.input_hash } });
  }
  // 一只都没辩成:不是"这批候选都不行",是这条链没跑起来。返回空名单等于把系统故障
  // 伪装成"今天没有推荐",调用方无从分辨,必须失败。最终决策人也无从选起。
  if (!scored.length) {
    const reasons = [...analystErrors, ...debateErrors].slice(0, 3).join("; ");
    throw new Error(`no stock survived the debate (${coarse.length} candidates)${reasons ? `: ${reasons}` : ""}`);
  }
  // 逐票风控结束后,最终决策人看完全部结论统一选名单。这是整场研判的最后一环:
  // 它必须给出 N 只——逐票各判各的,全判看空时一只都选不出(实测线上 17 只全看空)。
  abortIfCancelled();
  const pickPayload = {
    requested_final: request.limits.final,
    candidates: scored.map((entry) => ({
      ts_code: entry.verdict.ts_code,
      decision: entry.verdict.decision,
      score: entry.score,
      thesis: entry.verdict.reason,
      risk_control: entry.verdict.risk_control,
    })),
  };
  const pickParsed = await call(FINAL_PICK_ROLE, pickPayload);
  const pickReason = typeof pickParsed.reason === "string" ? pickParsed.reason.trim() : "";
  // 名单语义(用户确认):必须给满 N 只,哪怕全部看空——按评分选相对最优,
  // 收益对比数据才能持续积累。决策人的选择全部有效;不足 N 只时从全部已辩完的
  // 票里按风控评分降序补齐。全看空期名单代表"相对最优"而非"该买"。
  const scoredByCode = new Map(scored.map((entry) => [entry.verdict.ts_code, entry]));
  const pickCodes: string[] = Array.isArray(pickParsed.picks)
    ? pickParsed.picks.map((entry: Record<string, unknown>) => String(entry.ts_code ?? "")).filter((code: string) => code)
    : [];
  const picked = new Set(pickCodes.filter((code) => scoredByCode.has(code)));
  const allSorted = [...scored].sort((a, b) => b.score - a.score);
  for (const entry of allSorted) {
    if (picked.size >= request.limits.final) break;
    picked.add(entry.verdict.ts_code);
  }
  // 名单顺序:决策人选出的在前(按它的 rank 顺序),补齐的在后(按风控评分降序)。
  const finalOrder = [
    ...pickCodes.filter((code) => scoredByCode.has(code)),
    ...allSorted.map((entry) => entry.verdict.ts_code).filter((code) => !pickCodes.includes(code)),
  ].slice(0, request.limits.final);
  const final: JudgmentResult["final"] = finalOrder.map((code, index) => {
    const entry = scoredByCode.get(code)!;
    return { ...entry.verdict, rank: index + 1 };
  });
  const picks: JudgmentResult["picks"] = finalOrder.map((code, index) => ({
    ts_code: code,
    rank: index + 1,
    reason: pickReason || `${code} 入选`,
  }));
  ctx.emit({ type: "message.completed", data: { role: FINAL_PICK_ROLE, stage: "final_pick", ts_code: "", summary: pickReason, picks: finalOrder, input_hash: request.input_hash } });
  const output = { protocol_version: request.protocol_version, workflow_version: request.workflow_version, run_id: runId, trade_date: request.trade_date, candidate_hash: request.candidate_hash, input_hash: request.input_hash, coarse, deep, final, picks, usage };
  return validateJudgmentResult(output, request, runId);
}
