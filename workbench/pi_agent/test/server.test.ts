import test from "node:test";
import assert from "node:assert/strict";
import { createPiServer } from "../src/server.js";
import { FINAL_PICK_ROLE, computeCandidateHash, computeInputHash, validateJudgmentRequest, validateJudgmentResult } from "../src/contracts.js";
import { FauxProvider, roleSystemPrompt, type ProviderResponse, type RoleProvider, type ModelConfig } from "../src/provider.js";
import { runWorkflow } from "../src/workflow.js";
import { methodology } from "./fixtures.js";
type Deferred<T> = { promise: Promise<T>; resolve: (value: T | PromiseLike<T>) => void };
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const request = {
  protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
  limits: { coarse: 1, deep: 1, final: 1 }, candidates: [{ ts_code: "000000.SZ", name: "示例", score: 80 }], snapshots: [{ ts_code: "000000.SZ", name: "示例", quote: { close: 10 }, news: [] }], model: { provider: "faux", model: "faux", reasoning_effort: "low", max_tokens: 8192 },
  methodology
};
request.candidate_hash = computeCandidateHash(request.candidates);
request.input_hash = computeInputHash(request.candidates, request.snapshots);

test("health and authenticated idempotent run endpoints work", async (t) => {
  const app = createPiServer({ token: "test-token", provider: new FauxProvider() });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const base = `http://${address.host}:${address.port}`;
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  const health = await fetch(`${base}/internal/v1/health`, { headers });
  assert.equal(health.status, 200);
  const create = await fetch(`${base}/internal/v1/runs/run-1`, { method: "PUT", headers, body: JSON.stringify(request) });
  assert.equal(create.status, 202);
  const duplicate = await fetch(`${base}/internal/v1/runs/run-1`, { method: "PUT", headers, body: JSON.stringify(request) });
  assert.equal(duplicate.status, 200);
  const result = await waitForResult(base, headers);
  assert.equal(result.status, 200);
  const payload = await result.json();
  assert.equal(payload.run_id, "run-1");
});

test("rejects missing bearer token", async (t) => {
  const app = createPiServer({ token: "test-token", provider: new FauxProvider() });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const response = await fetch(`http://${address.host}:${address.port}/internal/v1/health`);
  assert.equal(response.status, 401);
});
test("rejects a request whose frozen candidate hash does not match its payload", async (t) => {
  const app = createPiServer({ token: "test-token", provider: new FauxProvider() });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  const response = await fetch(`http://${address.host}:${address.port}/internal/v1/runs/hash-mismatch`, { method: "PUT", headers, body: JSON.stringify({ ...request, candidate_hash: "0".repeat(64) }) });
  assert.equal(response.status, 400);
  assert.match(await response.text(), /candidate_hash/);
});

test("方法论前20全部进四轮辩论，按风控评分定出前3", async () => {
  const candidates = Array.from({ length: 20 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 100 - index }));
  const request = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
    limits: { coarse: 20, deep: 20, final: 3 }, candidates, snapshots: candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] })), model: { provider: "faux", model: "faux", max_tokens: 8192 },
    methodology
  } as const;
  Object.assign(request, { candidate_hash: computeCandidateHash(request.candidates), input_hash: computeInputHash(request.candidates, request.snapshots) });
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  const result = await runWorkflow("top20-run", request, new FauxProvider(), (event) => events.push(event), new AbortController().signal);
  const completed = events.filter((event) => event.type === "message.completed");
  const roles = completed.map((event) => String(event.data?.role));
  assert.equal(roles.filter((role) => role === "coarse").length, 0);
  assert.equal(roles.filter((role) => role === "technical").length, 0);
  assert.equal(roles.filter((role) => role === "methodology").length, 20);
  assert.equal(roles.filter((role) => role === "sentiment").length, 20);
  assert.equal(roles.filter((role) => role === "trend").length, 20);
  // debate 排序环节已删除：规则方法论已经排过一遍，再让模型排一次是多余的第二次排序，
  // 而它恰好是整条链最脆的一环（实测挂在这里，后面真正的辩论压根没开始）。
  assert.equal(roles.filter((role) => role === "debate").length, 0);
  // 四轮公开辩论：20 只**全部**参辩，各出一条 bull / bear / bull_counter / risk_chair。
  for (const role of ["bull", "bear", "bull_counter", "risk_chair"]) {
    assert.equal(roles.filter((entry) => entry === role).length, 20, `${role} 事件数不符`);
  }
  const debateEvents = completed.filter((event) => event.data?.stage === "debate" && ["bull", "bear", "bull_counter", "risk_chair"].includes(String(event.data?.role)));
  assert.ok(debateEvents.every((event) => Number(event.data?.round_no) >= 4), "辩论轮次编号必须接在三位分析师之后");
  assert.ok(debateEvents.every((event) => String(event.data?.ts_code).endsWith(".SZ")), "辩论事件必须带 ts_code");
  assert.equal(result.coarse.length, 20);
  assert.equal(result.deep.length, 20);
  assert.equal(result.final.length, 3);
  // 前三名由辩论评分产生，不是模型排序挑的，也不是规则分照抄。
  const scores = result.final.map((item) => item.score);
  assert.deepEqual(scores, scores.slice().sort((a, b) => b - a), "前三名必须按辩论评分降序");
  // 四段叙述必须来自对应轮次的真实产出，不是占位串。
  for (const item of result.final) {
    assert.equal(item.bull_case, `bull 论点 ${item.ts_code}`);
    assert.equal(item.bear_case, `bear 论点 ${item.ts_code}`);
    assert.equal(item.rebuttal, `bull_counter 论点 ${item.ts_code}`);
    assert.match(item.risk_control, /轻仓试错/);
    // 风控只能看多或看空：中性等于没给答案，而这一步的职责就是给答案。
    assert.ok(["看多", "看空"].includes(item.decision), `decision 不允许中性，实际 ${item.decision}`);
    assert.equal(item.reason, `${item.ts_code} 风控定稿`);
  }
});

/** 按 ts_code 指定风控方向的 provider：其余角色沿用 FauxProvider。 */
class VerdictProvider implements RoleProvider {
  private readonly inner = new FauxProvider();
  constructor(private readonly verdictOf: (code: string) => string) {}
  async complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> {
    if (role !== "risk_chair") return this.inner.complete(role, prompt, signal, brief, config);
    const parsed = JSON.parse(prompt) as Record<string, any>;
    const code = String(parsed.ts_code ?? "");
    return {
      text: JSON.stringify({ verdict: this.verdictOf(code), score: Number(parsed.score ?? 55), thesis: `${code} 风控定稿`, risks: ["波动"], action: "轻仓试错", citations: ["risk_chair:transcript"] }),
      inputTokens: 1, outputTokens: 2, finishReason: "stop"
    };
  }
}

test("最终决策人选出的名单优先看多,不足时按评分补齐", async () => {
  // 新契约:逐票风控之后,最终决策人看全部结论统一选名单。FauxProvider 的
  // final_pick 按 candidates 顺序取前 N,而 candidates 按 scored 的原始顺序
  // (规则分顺序)排列——所以决策人自然选到评分最高的三只。
  const { request, codes } = buildTop20Request();
  const result = await runWorkflow("pick-run", request, new VerdictProvider(() => "看多"), () => {}, new AbortController().signal);
  assert.equal(result.final.length, 3);
  assert.deepEqual(result.final.map((item) => item.ts_code), codes.slice(0, 3));
  // picks 与 final 一一对应,rank 连续。
  assert.equal(result.picks.length, 3);
  assert.deepEqual(result.picks.map((p) => p.ts_code), result.final.map((item) => item.ts_code));
  assert.deepEqual(result.picks.map((p) => p.rank), [1, 2, 3]);
  // 20 只全部辩完。
  assert.equal(result.deep.length, 20);
});

test("决策人没给满时按风控评分补齐到 N 只", async () => {
  // 决策人只选了 1 只(评分最高的),剩下 2 只按风控评分从全部已辩完的票里补齐。
  // 名单必须满员,否则无法和规则组比收益。
  const { request, codes } = buildTop20Request();
  class OnePickProvider extends FauxProvider {
    async complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> {
      const response = await super.complete(role, prompt, signal, brief, config);
      if (role !== FINAL_PICK_ROLE) return response;
      const parsed = JSON.parse(prompt) as { requested_final: number; candidates: Array<{ ts_code: string }> };
      const picks = parsed.candidates.slice(0, 1).map((entry, index) => ({ ts_code: entry.ts_code, rank: index + 1, reason: "只选这只" }));
      return { ...response, text: JSON.stringify({ picks, reason: "只给一只" }) };
    }
  }
  const result = await runWorkflow("one-pick-run", request, new OnePickProvider(), () => {}, new AbortController().signal);
  assert.equal(result.final.length, 3, "名单必须满员");
  // 决策人选的那只排第 1,补齐的按评分接在后面。
  assert.equal(result.final[0]!.ts_code, codes[0]);
  assert.equal(result.picks.length, 3);
});

test("决策人的选择全部有效,看空票也可入选", async () => {
  // 名单语义(用户确认):必须给满 N 只,哪怕全部看空——按评分选相对最优,
  // 收益对比数据才能持续积累。决策人把看空的头两名排前:该选择有效,补齐的
  // 从全部已辩完的票按评分取。
  const { request, codes } = buildTop20Request();
  const bearish = new Set(codes.slice(0, 2)); // 规则分最高的 2 只判看空
  class BearTopPickProvider extends VerdictProvider {
    constructor() { super((code) => (bearish.has(code) ? "看空" : "看多")); }
    async complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> {
      const response = await super.complete(role, prompt, signal, brief, config);
      if (role !== FINAL_PICK_ROLE) return response;
      const parsed = JSON.parse(prompt) as { requested_final: number; candidates: Array<{ ts_code: string }> };
      // 决策人选看空的头两名(它们评分最高)。
      const picks = parsed.candidates.slice(0, 2).map((entry, index) => ({ ts_code: entry.ts_code, rank: index + 1, reason: "回踩低吸" }));
      return { ...response, text: JSON.stringify({ picks, reason: "选相对最优" }) };
    }
  }
  const result = await runWorkflow("bear-pick-run", request, new BearTopPickProvider(), () => {}, new AbortController().signal);
  assert.equal(result.final.length, 3);
  // 决策人选的看空头两名排 1、2,补齐的看多最高分(规则分第 3 名)排 3。
  assert.deepEqual(result.final.map((item) => item.ts_code), [codes[0], codes[1], codes[2]]);
});

test("全部看空时决策人仍须给出满员名单", async () => {
  // 名单语义(用户确认):全看空也必须给满 N 只,按评分选相对最优,
  // 并在 reason 里说明是"相对最优"而非"看多推荐"。空名单不再合法。
  const { request } = buildTop20Request();
  const result = await runWorkflow("all-bearish-run", request, new VerdictProvider(() => "看空"), () => {}, new AbortController().signal);
  assert.equal(result.final.length, 3);
  assert.equal(result.deep.length, 20);
  assert.equal(result.picks.length, 3);
  for (const item of result.final) assert.equal(item.decision, "看空");
});



/** 造一个 20 只候选的合法请求，供并发相关用例复用。
 *
 * runWorkflow 收 unknown 后自己 validateJudgmentRequest，所以这里返回 unknown 就够，
 * 不需要把内部契约类型搬过来。ts_code 单独返回，省得调用方再从 unknown 里挖。
 */
function buildTop20Request(): { request: unknown; codes: string[] } {
  const candidates = Array.from({ length: 20 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 100 - index }));
  const snapshots = candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] }));
  const request = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13",
    candidate_hash: computeCandidateHash(candidates), input_hash: computeInputHash(candidates, snapshots),
    limits: { coarse: 20, deep: 20, final: 3 }, candidates, snapshots,
    model: { provider: "faux", model: "faux", max_tokens: 8192 }, methodology
  };
  return { request, codes: candidates.map((candidate) => candidate.ts_code) };
}

/** 包一层 FauxProvider，记录调用时序。实现 RoleProvider 而不是裸对象，
 *  签名走偏时编译期就会报，不用等运行时。 */
class InstrumentedProvider implements RoleProvider {
  inFlight = 0;
  peakInFlight = 0;
  calls = 0;
  private readonly inner = new FauxProvider();
  constructor(private readonly onCall?: (calls: number) => void) {}
  async complete(role: string, prompt: string, signal: AbortSignal, brief?: string, config?: ModelConfig): Promise<ProviderResponse> {
    this.calls += 1;
    this.inFlight += 1;
    this.peakInFlight = Math.max(this.peakInFlight, this.inFlight);
    this.onCall?.(this.calls);
    // 让出事件循环，给其他 worker 机会跑起来——否则纯同步返回测不出并发。
    await new Promise<void>((resolve) => setTimeout(resolve, 5));
    this.inFlight -= 1;
    return this.inner.complete(role, prompt, signal, brief, config);
  }
}

test("20 只分析与辩论并发执行，结果顺序与串行一致", async () => {
  // 实测串行跑 20 只要 2.5 小时（辩论平均 5.7 分钟/只），瓶颈全在等模型返回。
  // 这个用例证明两件事：真的并发了（同时在飞的调用数 > 1），且并发没打乱结果顺序。
  const { request, codes } = buildTop20Request();
  const provider = new InstrumentedProvider();
  const result = await runWorkflow("concurrent-run", request, provider, () => {}, new AbortController().signal);
  assert.ok(provider.peakInFlight > 1, `并发没生效，峰值在飞调用数 ${provider.peakInFlight}`);
  assert.ok(provider.peakInFlight <= 16, `并发无上限会撞上游限速，峰值 ${provider.peakInFlight}`);
  // deep 顺序必须仍是规则分顺序：并发只改执行时序，不改结果编排。
  assert.deepEqual(result.deep.map((item) => item.ts_code), codes);
  assert.deepEqual(result.deep.map((item) => item.rank), Array.from({ length: 20 }, (_, index) => index + 1));
  assert.equal(result.final.length, 3);
});

test("并发下取消后不再发起新的模型调用", async () => {
  // 取消是外部指令，必须一票否决：不能把"用户点了停止"记成 20 条个股失败，
  // 也不能在取消后继续烧配额把剩下的候选跑完。
  const controller = new AbortController();
  const provider = new InstrumentedProvider((calls) => { if (calls === 3) controller.abort(); });
  await assert.rejects(
    () => runWorkflow("cancel-run", buildTop20Request().request, provider, () => {}, controller.signal),
    (error: unknown) => error instanceof DOMException && error.name === "AbortError"
  );
  // 20 只 × 3 位分析师 = 60 次调用。取消后只允许在飞的那几路收尾，
  // 绝不能把 60 次跑完——留足余量，但远低于 60。
  assert.ok(provider.calls < 20, `取消后仍在发起新调用，共 ${provider.calls} 次`);
});

test("batch workflow skips one candidate whose analyst call fails", async () => {
  // 原来这里用只含 1 只候选的 request，"跳过唯一一只"其实等于全军覆没，验不出
  // "一只挂了其余照跑"。改用 3 只：挂掉第一只，剩下 2 只必须正常辩完。
  const candidates = Array.from({ length: 3 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 90 - index }));
  const partialRequest = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13",
    candidate_hash: computeCandidateHash(candidates), input_hash: "",
    limits: { coarse: 3, deep: 3, final: 3 }, candidates,
    snapshots: candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] })),
    model: { provider: "faux", model: "faux", max_tokens: 8192 }, methodology,
  };
  partialRequest.input_hash = computeInputHash(partialRequest.candidates, partialRequest.snapshots);
  const faux = new FauxProvider();
  let sentimentCalls = 0;
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => {
    if (role === "sentiment" && sentimentCalls++ === 0) throw new Error("single candidate failed");
    return faux.complete(role, prompt, signal);
  } };
  const events: Array<{ type: string }> = [];

  const result = await runWorkflow("partial-run", partialRequest, provider, (event) => events.push(event), new AbortController().signal);

  assert.equal(result.deep.length, 2);
  assert.equal(result.final.length, 2);
  assert.ok(events.some((event) => event.type === "message.failed"));
});
test("batch workflow skips one candidate whose analyst payload is invalid", async () => {
  const faux = new FauxProvider();
  let methodologyCalls = 0;
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => {
    if (role === "methodology" && methodologyCalls++ === 0) {
      return { text: JSON.stringify({ stance: "观望", conclusion: "字段不合法", risks: [] }), finishReason: "stop" };
    }
    return faux.complete(role, prompt, signal);
  } };
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  // request 只有一只候选，它废了就是全废：返回空名单会让调用方以为"跑完了没选出"。
  await assert.rejects(
    () => runWorkflow("invalid-analyst", request, provider, (event) => events.push(event), new AbortController().signal),
    /no stock survived the debate \(1 candidates\): 000000\.SZ: methodology\.stance/
  );
  assert.ok(events.some((event) => event.type === "message.failed" && event.data?.ts_code === "000000.SZ"));
});

test("plain-text analyst output is rejected instead of synthesized", async () => {
  const candidates = Array.from({ length: 20 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 100 - index }));
  const relaxedRequest = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
    limits: { coarse: 20, deep: 20, final: 3 }, candidates, snapshots: candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] })), model: { provider: "faux", model: "faux", max_tokens: 8192 },
    methodology
  } as const;
  Object.assign(relaxedRequest, { candidate_hash: computeCandidateHash(relaxedRequest.candidates), input_hash: computeInputHash(relaxedRequest.candidates, relaxedRequest.snapshots) });
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => {
    if (["methodology", "sentiment", "trend"].includes(role)) return { text: `${role} 普通文字分析`, inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    // debate 排序环节已删除，不再有这个角色。
    return new FauxProvider().complete(role, prompt, signal);
  } };
  const events: Array<{ type: string }> = [];
  // 分析师结论要被程序按 stance 字段消费，纯文本不能用。20 只全废不是"今天没推荐"，
  // 是这条链没跑起来，必须失败而不是返回空名单。
  await assert.rejects(
    () => runWorkflow("strict-run", relaxedRequest, provider, (event) => events.push(event), new AbortController().signal),
    /no stock survived the debate \(20 candidates\)/
  );
  assert.equal(events.filter((event) => event.type === "message.failed").length, 20);
});

test("辩论论点解析不出 JSON 时整段文本就是论点", async () => {
  const faux = new FauxProvider();
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => role === "bear"
    ? { text: "这只票量能萎缩、上方套牢盘重，短线不宜追高。", inputTokens: 1, outputTokens: 2, finishReason: "stop" }
    : faux.complete(role, prompt, signal) };
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  // 辩论产出是给人读的文本，不是要被程序按字段消费的结构。模型讲清了道理却没套 JSON
  // 外壳，把这只股票整个丢掉是削自己的脚适履——让它们辩起来才是目的。
  const result = await runWorkflow("debate-text", request, provider, (event) => events.push(event), new AbortController().signal);
  assert.equal(result.final.length, 1);
  assert.equal(result.final[0].bear_case, "这只票量能萎缩、上方套牢盘重，短线不宜追高。");
});

test("辩论一句话都没说才失败，不补占位文本", async () => {
  const faux = new FauxProvider();
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => role === "bear"
    ? { text: "   ", inputTokens: 1, outputTokens: 2, finishReason: "stop" }
    : faux.complete(role, prompt, signal) };
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  // 空回复没有任何可存的内容，编一句占位文本就是造假。
  await assert.rejects(
    () => runWorkflow("debate-empty", request, provider, (event) => events.push(event), new AbortController().signal),
    /no stock survived the debate \(1 candidates\): 000000\.SZ: bear returned no output/,
  );
  assert.ok(events.some((event) => event.type === "message.failed" && /bear/.test(String(event.data?.error))));
});

test("风控主席只能看多或看空，中性视为没给答案", async () => {
  const faux = new FauxProvider();
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => role === "risk_chair"
    ? { text: JSON.stringify({ verdict: "中性", score: 55, thesis: "多空均衡", risks: ["波动"], action: "观望", citations: [] }), inputTokens: 1, outputTokens: 2, finishReason: "stop" }
    : faux.complete(role, prompt, signal) };
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  // 风控定稿的职责就是给出方向。允许中性等于允许它交白卷，而这条名单是要拿来
  // 和规则组比收益的——一份全是"中性"的名单比不出任何东西。
  await assert.rejects(
    () => runWorkflow("verdict-neutral", request, provider, (event) => events.push(event), new AbortController().signal),
    /no stock survived the debate \(1 candidates\): 000000\.SZ: risk_chair returned invalid verdict: 中性/,
  );
  assert.ok(events.some((event) => event.type === "message.failed" && /verdict/.test(String(event.data?.error))));
});

test("方法论职责与正文都进入每个角色的系统提示词", async () => {
  const seen = new Map<string, string>();
  const faux = new FauxProvider();
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal, brief?: string) => {
    seen.set(role, roleSystemPrompt(role, brief));
    return faux.complete(role, prompt, signal);
  } };
  await runWorkflow("brief-run", request, provider, () => {}, new AbortController().signal);
  for (const role of ["methodology", "sentiment", "trend", "bull", "bear", "bull_counter", "risk_chair"]) {
    const prompt = seen.get(role);
    assert.ok(prompt, `${role} 未被调用`);
    assert.match(prompt!, /情绪周期七阶段/);
    assert.ok(prompt!.includes(`${role} 角色职责说明`), `${role} 缺少角色职责`);
  }
});

test("analyst prompt requires the strict JSON stance contract", () => {
  const prompt = roleSystemPrompt("methodology");
  assert.match(prompt, /"stance":"bull\|bear\|neutral"/);
  assert.match(prompt, /JSON only/);
});

test("辩手提示词必须给论点长度上限，否则输出被 max_tokens 截断", () => {
  // 实测证据：一次完整运行 20 只里 7 只失败，原因全是
  // "bull/bear/bull_counter returned truncated output (max_tokens)"。
  // max_tokens 已经是模型硬上限 8192，加不上去了；而 risk_chair 有 "under 80
  // characters" 约束，一次都没截断过。差别就在提示词有没有给长度上限——
  // 辩手拿到完整 transcript 逐条反驳，不给界它就一直写。
  for (const role of ["bull", "bear", "bull_counter"]) {
    const prompt = roleSystemPrompt(role);
    assert.match(prompt, /under \d+ characters/, `${role} 提示词缺少论点长度上限`);
  }
});

test("provider failures are persisted as failed runs and never expose a result", async (t) => {
  const provider = { complete: async () => { throw new Error("provider unavailable"); } };
  const app = createPiServer({ token: "test-token", provider });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const base = `http://${address.host}:${address.port}`;
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  const create = await fetch(`${base}/internal/v1/runs/provider-failed`, { method: "PUT", headers, body: JSON.stringify(request) });
  assert.equal(create.status, 202);
  const result = await waitForResult(base, headers, "provider-failed");
  assert.equal(result.status, 409);
  const payload = await result.json() as { status: string; error?: string };
  assert.equal(payload.status, "failed");
  assert.match(payload.error ?? "", /provider unavailable/);
});

test("池外股票混进最终名单会被校验拒绝，不会当成功结果返回", () => {
  // debate 排序角色已删除，模型不再有机会"选出"一只池外股票。但结果校验这层约束必须
  // 留着：任何路径伪造出池外代码，都不能变成一份看起来正常的名单落进台账。
  const outsider = {
    protocol_version: request.protocol_version, workflow_version: request.workflow_version, run_id: "invalid-pool",
    trade_date: request.trade_date, candidate_hash: request.candidate_hash, input_hash: request.input_hash,
    coarse: [{ ts_code: "999999.SZ", rank: 1, score: 80, reason: "方法论候选" }], deep: [], final: [],
    usage: { input_tokens: 0, output_tokens: 0 },
  };
  assert.throws(() => validateJudgmentResult(outsider, validateJudgmentRequest(request), "invalid-pool"), /coarse subset/);
});

test("断流重试之间必须退避，不能连打三次", async () => {
  // 上游限流的表现就是断流。连打三次几乎必然三次都断：实测一轮 20 只候选里 15 只
  // 在第一个角色调用上连续断流三次，19 分钟产出 0 份研判。退避是让重试真有机会成功。
  const waits: number[] = [];
  let attempts = 0;
  const provider = { complete: async () => {
    attempts += 1;
    return { text: "", inputTokens: 0, outputTokens: 0, finishReason: "error" };
  } };
  await assert.rejects(
    () => runWorkflow("backoff-run", request, provider, () => {}, new AbortController().signal,
      (attempt) => { waits.push(attempt); return 0; }),
    /no stock survived the debate/
  );
  // 三次尝试之间退避两次：第 1 次失败后等一次，第 2 次失败后等一次，第 3 次直接抛。
  assert.equal(attempts, 3, "断流必须重试到 3 次");
  assert.deepEqual(waits, [1, 2], "退避必须发生在前两次失败之后，且带上尝试序号");
});

test("退避时长按尝试次数指数增长", async () => {
  // 固定间隔碰上限流窗口会连续撞墙，指数退避才能跳出窗口。
  const seen: number[] = [];
  const provider = { complete: async () => ({ text: "", inputTokens: 0, outputTokens: 0, finishReason: "error" }) };
  await assert.rejects(
    () => runWorkflow("backoff-shape", request, provider, () => {}, new AbortController().signal,
      (attempt) => { const ms = 2000 * 2 ** (attempt - 1); seen.push(ms); return 0; }),
    /no stock survived the debate/
  );
  assert.deepEqual(seen, [2000, 4000], "默认退避应为 2s、4s");
});

test("runs execute serially and cancellation records a terminal cancelled state", async (t) => {
  let active = 0;
  let maxActive = 0;
  const firstStarted = deferred<void>();
  const releaseFirst = deferred<void>();
  let calls = 0;
  const faux = new FauxProvider();
  const provider = {
    complete: async (role: string, prompt: string, signal: AbortSignal) => {
      calls += 1;
      active += 1;
      maxActive = Math.max(maxActive, active);
      try {
        if (calls === 1) {
          firstStarted.resolve();
          await Promise.race([releaseFirst.promise, new Promise<never>((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }))]);
        }
        return await faux.complete(role, prompt, signal);
      } finally {
        active -= 1;
      }
    },
  };
  const app = createPiServer({ token: "test-token", provider });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const base = `http://${address.host}:${address.port}`;
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  const first = await fetch(`${base}/internal/v1/runs/serial-1`, { method: "PUT", headers, body: JSON.stringify(request) });
  assert.equal(first.status, 202);
  await firstStarted.promise;
  const second = await fetch(`${base}/internal/v1/runs/serial-2`, { method: "PUT", headers, body: JSON.stringify(request) });
  assert.equal(second.status, 202);
  assert.equal(calls, 1);
  const cancelled = await fetch(`${base}/internal/v1/runs/serial-1/cancel`, { method: "POST", headers });
  assert.equal(cancelled.status, 202);
  const cancelledResult = await waitForResult(base, headers, "serial-1");
  assert.equal(cancelledResult.status, 409);
  assert.equal((await cancelledResult.json()).status, "cancelled");
  releaseFirst.resolve();
  const secondResult = await waitForResult(base, headers, "serial-2");
  assert.equal(secondResult.status, 200);
  assert.equal(maxActive, 1);
});

test("SSE history resume starts after source sequence without duplicates", async (t) => {
  const app = createPiServer({ token: "test-token", provider: new FauxProvider() });
  const address = await app.listen(0, "127.0.0.1");
  t.after(() => app.close());
  const base = `http://${address.host}:${address.port}`;
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  await fetch(`${base}/internal/v1/runs/sse-1`, { method: "PUT", headers, body: JSON.stringify(request) });
  await waitForResult(base, headers, "sse-1");
  const first = await fetch(`${base}/internal/v1/runs/sse-1/events`, { headers });
  const firstText = await first.text();
  const firstIds = [...firstText.matchAll(/^id: (\d+)$/gm)].map((match) => Number(match[1]));
  assert.ok(firstIds.length >= 2);
  const after = firstIds.at(-2)!;
  const resumed = await fetch(`${base}/internal/v1/runs/sse-1/events?after_source_seq=${after}`, { headers });
  const resumedIds = [...(await resumed.text()).matchAll(/^id: (\d+)$/gm)].map((match) => Number(match[1]));
  assert.ok(resumedIds.length > 0);
  assert.ok(resumedIds.every((id) => id > after));
  assert.equal(new Set(resumedIds).size, resumedIds.length);
});

test("长间隔的运行中事件流靠心跳保持有字节流出", async (t) => {
  // 真实模型单个角色可跑 100 秒以上，事件之间没有字节会让客户端读超时把健康的流掐断。
  // 这里把角色调用挂住，只验证心跳帧仍在流出：没有心跳，这个断言会因为读不到字节而超时失败。
  let release: (() => void) | null = null;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  const provider = {
    complete: async () => {
      await blocked;
      throw new Error("released");
    },
  };
  const app = createPiServer({ token: "test-token", provider, heartbeatMs: 20 });
  const address = await app.listen(0, "127.0.0.1");
  t.after(async () => {
    release?.();
    await app.close();
  });
  const base = `http://${address.host}:${address.port}`;
  const headers = { authorization: "Bearer test-token", "content-type": "application/json" };
  await fetch(`${base}/internal/v1/runs/sse-heartbeat`, { method: "PUT", headers, body: JSON.stringify(request) });
  const stream = await fetch(`${base}/internal/v1/runs/sse-heartbeat/events`, { headers });
  const reader = stream.body!.getReader();
  const decoder = new TextDecoder();
  let text = "";
  // 这里刻意用真实时钟：被验证的对象就是 SSE 心跳在真实平台时钟上的输出行为，
  // 假时钟无法证明 socket 上真的有字节流出。心跳设成 20ms，上限 3 秒，代价可忽略。
  // 没有心跳时 read() 永不返回，必须自己设上限，让缺陷表现为断言失败而不是挂死。
  const deadline = Date.now() + 3000;
  while (!text.includes(": keep-alive") && Date.now() < deadline) {
    const chunk = await Promise.race([
      reader.read(),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), Math.max(1, deadline - Date.now()))),
    ]);
    if (chunk === null || chunk.done) break;
    text += decoder.decode(chunk.value, { stream: true });
  }
  await reader.cancel().catch(() => {});
  assert.ok(text.includes(": keep-alive"), "运行中的事件流必须持续输出心跳帧");
});

async function waitForResult(base: string, headers: Record<string, string>, runId = "run-1"): Promise<Response> {
  for (let i = 0; i < 50; i += 1) {
    const response = await fetch(`${base}/internal/v1/runs/${runId}/result`, { headers });
    if (response.status !== 202) return response;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed out waiting for result");
}
