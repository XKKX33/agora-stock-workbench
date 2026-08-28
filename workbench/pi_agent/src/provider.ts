import { Agent, type StreamFn } from "@earendil-works/pi-agent-core";
import { contentText, type Model } from "@earendil-works/pi-ai";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { ANALYST_ROLES, FINAL_PICK_ROLE } from "./contracts.js";

export interface ProviderResponse { text: string; inputTokens?: number; outputTokens?: number; finishReason?: string }

/** 单次调用的模型配置，来自 Python 随请求下发的 request.model。 */
export interface ModelConfig { provider: string; model: string; reasoning_effort?: string; max_tokens: number }

export interface RoleProvider { complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> }

export function jsonResponseFormat(): { type: "json_object" } { return { type: "json_object" }; }

const DEBATERS: readonly string[] = ["bull", "bear", "bull_counter"];

/** 输出 schema 契约。方法论正文与角色职责由 Python 随请求下发（brief），此处只约束 JSON 形状。 */
export function roleSchemaPrompt(role: string): string {
  if ((ANALYST_ROLES as readonly string[]).includes(role)) {
    return `You are the ${role} stock analyst. Return JSON only and match this exact schema: {"stance":"bull|bear|neutral","conclusion":"string","risks":["string"]}. stance must be exactly bull, bear, or neutral. conclusion must state your actual judgement in Chinese. Do not add markdown fences or extra fields.`;
  }
  if (DEBATERS.includes(role)) {
    // 论点是给人读的文本：JSON 只是首选形状，纯文本回复 workflow 也照收（见
    // debaterArgument）。这里仍然要求 JSON，是为了让 citations 有地方放。
    //
    // 必须给长度上限。实测一次 20 只的运行里 7 只死在
    // "returned truncated output (max_tokens)"，而 max_tokens 已经是模型硬上限 8192；
    // 同一次运行里 risk_chair 一次都没截断，差别就是它的 thesis 写了 "under 80
    // characters"。辩手拿到完整 transcript 逐条反驳，不给界它就一直写到配额耗尽。
    // 300 字足够讲清一个论点，也给推理 token 留出余量。
    return `You are the ${role} debater in a public transcript. Prefer JSON matching this schema: {"argument":"string","citations":["string"]}. argument must be your actual case in Chinese, under 300 characters, citing the transcript entries or snapshot fields it rests on. Be concise: state your strongest points, do not restate the whole transcript. citations lists those sources. If you cannot produce JSON, reply with your argument as plain Chinese prose under 300 characters. Never invent facts absent from the input.`;
  }
  if (role === "risk_chair") {
    // 只能看多或看空：这份名单要拿去和规则组比收益，一份全是"中性"的名单比不出东西。
    return `You are the risk chair closing the public debate. Return JSON only and match this exact schema: {"verdict":"看多|看空","score":0,"thesis":"string","risks":["string"],"action":"string","citations":["string"]}. verdict must be exactly 看多 or 看空 — 中性 is not allowed, you must commit to a direction. score is an integer from 0 to 100 ranking this stock's short-term attractiveness; it decides which stocks make the final list, so score honestly and differentiate. thesis is the core logic in Chinese, under 80 characters. action is the concrete short-term risk_control instruction in Chinese, under 40 characters. Never invent facts absent from the input. Do not add markdown fences or extra fields.`;
  }
  if (role === FINAL_PICK_ROLE) {
    // 最终决策人:必须给出恰好 N 只。N 在输入的 requested_final 里,提示词里写死
    // "exactly N" 不行——N 是运行时才知道的,靠 schema 里的占位说明 + workflow 侧
    // 补齐逻辑双保险:决策人少给了按评分补,给多了按它的顺序截断。
    return `You are the final decision maker closing the entire research session. Input lists every candidate with its risk verdict (decision/score/thesis/risk_control) and requested_final N. Return JSON only and match this exact schema: {"picks":[{"ts_code":"string","rank":1,"reason":"string"}],"reason":"string"}. picks must contain exactly N entries — no more, no fewer — ranked 1..N by short-term potential, best first. Prefer stocks the risk chair marked 看多; compare scores and argument quality among equals. Only include a 看空 stock if you are confident the risk chair misjudged it, and say why in its reason. reason is the overall rationale in Chinese, under 120 characters. Never invent facts absent from the input. Do not add markdown fences or extra fields.`;
  }
  throw new Error(`unknown role: ${role}`);
}

/** 组装系统提示词：Python 下发的方法论与角色职责在前，TS 的输出 schema 契约在后。 */
export function roleSystemPrompt(role: string, brief?: string): string {
  const schema = roleSchemaPrompt(role);
  return brief && brief.trim() ? `${brief.trim()}\n${schema}` : schema;
}

export class FauxProvider implements RoleProvider {
  // 参数列表与 RoleProvider 保持一致：假 provider 不看 brief/config，但测试会包一层
  // 转发全部参数，签名缺参会在编译期报错。
  async complete(role: string, prompt: string, signal: AbortSignal, _brief?: string, _config?: ModelConfig): Promise<ProviderResponse> {
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    const parsed = JSON.parse(prompt) as Record<string, any>;
    const code = parsed.ts_code ?? "000000.SZ";
    const done = (payload: Record<string, unknown>): ProviderResponse => ({ text: JSON.stringify(payload), inputTokens: 1, outputTokens: 2, finishReason: "stop" });
    if ((ANALYST_ROLES as readonly string[]).includes(role)) {
      return done({ stance: role === "sentiment" ? "neutral" : "bull", conclusion: `${role}分析`, risks: [] });
    }
    if (DEBATERS.includes(role)) {
      return done({ argument: `${role} 论点 ${code}`, citations: [`${role}:transcript`] });
    }
    if (role === "risk_chair") {
      // 评分随传入的规则分走：辩论评分决定谁进前三，测试要能验证排序真的按它来，
      // 所有股票同一个分数就验不出排序。中性已被禁止，这里给看多。
      return done({ verdict: "看多", score: Number(parsed.score ?? 55), thesis: `${code} 风控定稿`, risks: ["波动"], action: "轻仓试错", citations: ["risk_chair:transcript"] });
    }
    if (role === FINAL_PICK_ROLE) {
      // 测试要能验证决策人真的在选:从输入里取 requested_final 与候选,按给定顺序选满。
      const wanted = Number(parsed.requested_final ?? 3);
      const pool: string[] = (parsed.candidates ?? []).map((entry: Record<string, unknown>) => String(entry.ts_code));
      const picks = pool.slice(0, wanted).map((code, index) => ({ ts_code: code, rank: index + 1, reason: `${code} 入选` }));
      return done({ picks, reason: `按短线潜力选 ${wanted} 只` });
    }
    throw new Error(`FauxProvider 收到未知角色: ${role}`);
  }
}

/** reasoning_effort 是 Python 下发的自由字符串，收窄成 Agent 接受的枚举。 */
const THINKING_LEVELS = ["low", "medium", "high"] as const;

/**
 * 单次模型调用上限。实测单角色最长约 140 秒(辩论轮带完整 transcript),
 * 取 300 秒留足余量:低于实测值会误杀正常调用,不设上限则上游卡死时整条流程静默挂住。
 */
const CALL_TIMEOUT_MS = 300_000;


export interface PiAgentAdapterOptions { model?: Model<"openai-completions"> }
export class PiAgentProvider implements RoleProvider {
  private readonly model: Model<"openai-completions">;
  private readonly apiKey: string;
  constructor(private readonly options: PiAgentAdapterOptions = {}) {
    this.apiKey = process.env.PI_AGENT_API_KEY ?? process.env.MINIMAX_API_KEY ?? "";
    this.model = options.model ?? {
      id: process.env.PI_AGENT_MODEL ?? "minimax-m3", name: "MiniMax M3", api: "openai-completions", provider: "openai-compatible",
      baseUrl: process.env.PI_AGENT_BASE_URL ?? "https://api.pie-xian.com/v1", reasoning: true, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32768, maxTokens: 8192,
      compat: { supportsReasoningEffort: true, maxTokensField: "max_tokens", supportsUsageInStreaming: true }
    };
  }

  async complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> {
    if (!this.apiKey) throw new Error("PI_AGENT_API_KEY is required");
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    // Python 是模型配置的唯一来源，环境变量只作为独立调试时的兜底。不落地
    // max_tokens 的话模型按 maxTokens: 8192 放开写，辩论轮输出过长、长流式中途断流。
    const model: Model<"openai-completions"> = config
      ? { ...this.model, id: config.model || this.model.id, maxTokens: config.max_tokens }
      : this.model;
    const thinkingLevel = THINKING_LEVELS.find((level) => level === config?.reasoning_effort) ?? "low";
    const agent = new Agent({
      initialState: { systemPrompt: roleSystemPrompt(role, brief), model, thinkingLevel, tools: [] },
      // streamFn 的形参类型由 pi-agent-core 用宽泛的 Model 声明；这里只服务
      // openai-completions 一种 api，收窄到具体类型后交给 streamSimple。
      streamFn: ((m: Model<"openai-completions">, context, options) => streamSimple(m, context, {
        ...options,
        samplingParams: {
          ...(options?.samplingParams ?? {}),
          response_format: jsonResponseFormat(),
          max_tokens: model.maxTokens,
        }
      })) as StreamFn,
      getApiKey: () => this.apiKey,
      toolExecution: "sequential"
    });
    // 只挂取消信号是不够的:上游卡住不返回时这里会无限等待,整条流程静默挂死
    // (实测卡过一小时,进度停在某一轮不动,既不失败也不推进)。给单次调用设上限,
    // 超时就中止并抛错,交给上层重试;死等没有任何价值。
    const onAbort = () => agent.abort(); signal.addEventListener("abort", onAbort, { once: true });
    let timer: NodeJS.Timeout | undefined;
    let timedOut = false;
    try {
      const timeout = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          agent.abort();
          reject(new Error(`${role} timed out after ${CALL_TIMEOUT_MS / 1000}s`));
        }, CALL_TIMEOUT_MS);
      });
      await Promise.race([agent.prompt(prompt), timeout]);
      const message = agent.state.messages.at(-1);
      if (!message || message.role !== "assistant") throw new Error("Pi agent returned no assistant message");
      return { text: contentText(message.content), inputTokens: message.usage.input, outputTokens: message.usage.output, finishReason: message.stopReason };
    } catch (error) {
      // abort() 可能让 prompt 先以 AbortError 结束、抢在超时 reject 之前落地,
      // 统一成超时错误,避免上层看到语义不明的 AbortError。
      if (timedOut) throw new Error(`${role} timed out after ${CALL_TIMEOUT_MS / 1000}s`);
      throw error;
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
    }
  }
}
