import test from "node:test";
import assert from "node:assert/strict";
import { createPiServer } from "../src/server.js";
import { computeCandidateHash, computeInputHash } from "../src/contracts.js";
import { FauxProvider, roleSystemPrompt } from "../src/provider.js";
import { runWorkflow } from "../src/workflow.js";
type Deferred<T> = { promise: Promise<T>; resolve: (value: T | PromiseLike<T>) => void };
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const request = {
  protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
  limits: { coarse: 1, deep: 1, final: 1 }, candidates: [{ ts_code: "000000.SZ", name: "示例", score: 80 }], snapshots: [{ ts_code: "000000.SZ", name: "示例", quote: { close: 10 }, news: [] }], model: { provider: "faux", model: "faux", reasoning_effort: "low", max_tokens: 8192 }
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

test("batch workflow analyzes all methodology top20 and debates directly to final3", async () => {
  const candidates = Array.from({ length: 20 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 100 - index }));
  const request = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
    limits: { coarse: 20, deep: 20, final: 3 }, candidates, snapshots: candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] })), model: { provider: "faux", model: "faux", max_tokens: 8192 }
  } as const;
  Object.assign(request, { candidate_hash: computeCandidateHash(request.candidates), input_hash: computeInputHash(request.candidates, request.snapshots) });
  const events: Array<{ type: string; data?: Record<string, unknown> }> = [];
  const result = await runWorkflow("top20-run", request, new FauxProvider(), (event) => events.push(event), new AbortController().signal);
  const roles = events.filter((event) => event.type === "message.completed").map((event) => String(event.data?.role));
  assert.equal(roles.filter((role) => role === "coarse").length, 0);
  assert.equal(roles.filter((role) => role === "methodology").length, 20);
  assert.equal(roles.filter((role) => role === "sentiment").length, 20);
  assert.equal(roles.filter((role) => role === "technical").length, 20);
  assert.equal(roles.filter((role) => role === "debate").length, 3);
  assert.equal(result.coarse.length, 20);
  assert.equal(result.deep.length, 20);
  assert.equal(result.final.length, 3);
  assert.ok(result.final.every((item) => item.bull_case && item.bear_case && item.rebuttal && item.risk_control));
});

test("batch workflow skips one candidate whose analyst call fails", async () => {
  const faux = new FauxProvider();
  let sentimentCalls = 0;
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => {
    if (role === "sentiment" && sentimentCalls++ === 0) throw new Error("single candidate failed");
    return faux.complete(role, prompt, signal);
  } };
  const events: Array<{ type: string }> = [];

  const result = await runWorkflow("partial-run", request, provider, (event) => events.push(event), new AbortController().signal);

  assert.equal(result.deep.length, request.candidates.length - 1);
  assert.equal(result.final.length, Math.min(request.limits.final, result.deep.length));
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

  const result = await runWorkflow("invalid-analyst", request, provider, (event) => events.push(event), new AbortController().signal);

  assert.equal(result.deep.length, 0);
  assert.equal(result.final.length, 0);
  assert.ok(events.some((event) => event.type === "message.failed" && event.data?.ts_code === "000000.SZ"));
});

test("plain-text analyst output is rejected instead of synthesized", async () => {
  const candidates = Array.from({ length: 20 }, (_, index) => ({ ts_code: `${String(index).padStart(6, "0")}.SZ`, name: `股票${index}`, score: 100 - index }));
  const relaxedRequest = {
    protocol_version: "1", workflow_version: "1", mode: "batch", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
    limits: { coarse: 20, deep: 20, final: 3 }, candidates, snapshots: candidates.map((candidate) => ({ ...candidate, quote: { close: 10 }, news: [] })), model: { provider: "faux", model: "faux", max_tokens: 8192 }
  } as const;
  Object.assign(relaxedRequest, { candidate_hash: computeCandidateHash(relaxedRequest.candidates), input_hash: computeInputHash(relaxedRequest.candidates, relaxedRequest.snapshots) });
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => {
    if (["methodology", "sentiment", "technical"].includes(role)) return { text: `${role} 普通文字分析`, inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    if (role === "debate") return { text: JSON.stringify({ selected: [] }), inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    return new FauxProvider().complete(role, prompt, signal);
  } };
  const events: Array<{ type: string }> = [];
  const result = await runWorkflow("strict-run", relaxedRequest, provider, (event) => events.push(event), new AbortController().signal);
  assert.equal(result.deep.length, 0);
  assert.equal(result.final.length, 0);
  assert.equal(events.filter((event) => event.type === "message.failed").length, 20);
});

test("analyst prompt requires the strict JSON stance contract", () => {
  const prompt = roleSystemPrompt("methodology");
  assert.match(prompt, /"stance":"bull\|bear\|neutral"/);
  assert.match(prompt, /JSON only/);
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

test("unknown debate stock fails the whole run without a successful result", async () => {
  const faux = new FauxProvider();
  const provider = { complete: async (role: string, prompt: string, signal: AbortSignal) => role === "debate"
    ? { text: JSON.stringify({ selected: [{ ts_code: "999999.SZ", decision: "buy", score: 99, bull_case: "invalid", bear_case: "invalid", rebuttal: "invalid", risk_control: "invalid" }] }), finishReason: "stop" }
    : faux.complete(role, prompt, signal) };
  const events: Array<{ type: string }> = [];
  await assert.rejects(() => runWorkflow("invalid-pool", request, provider, (event) => events.push(event), new AbortController().signal), /unknown candidate/);
  assert.ok(events.some((event) => event.type === "run.started"));
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

async function waitForResult(base: string, headers: Record<string, string>, runId = "run-1"): Promise<Response> {
  for (let i = 0; i < 50; i += 1) {
    const response = await fetch(`${base}/internal/v1/runs/${runId}/result`, { headers });
    if (response.status !== 202) return response;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed out waiting for result");
}
