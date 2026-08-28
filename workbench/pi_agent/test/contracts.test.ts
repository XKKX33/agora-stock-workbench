import test from "node:test";
import assert from "node:assert/strict";
import { validateJudgmentRequest, validateJudgmentResult, validateMethodology, type JudgmentRequest } from "../src/contracts.js";
import { methodology } from "./fixtures.js";

const validRequest: JudgmentRequest = {
  protocol_version: "1",
  workflow_version: "1",
  mode: "single",
  trade_date: "2026-08-13",
  candidate_hash: "candidate-hash",
  input_hash: "input-hash",
  limits: { coarse: 1, deep: 1, final: 1 },
  candidates: Array.from({ length: 1 }, (_, i) => ({ ts_code: `00000${i}.SZ`, name: "示例" })),
  snapshots: [{ ts_code: "000000.SZ", name: "示例", quote: { close: 10 }, news: [] }],
  model: { provider: "openai-compatible", model: "minimax-m3", reasoning_effort: "low", max_tokens: 8192 },
  methodology
};

test("rejects requests missing a frozen input hash", () => {
  const request = { ...validRequest, input_hash: "" };
  assert.throws(() => validateJudgmentRequest(request), /input_hash/);
});

test("rejects a methodology payload missing a role brief", () => {
  const { bull_counter, ...incomplete } = methodology.role_briefs;
  assert.throws(() => validateMethodology({ ...methodology, role_briefs: incomplete }), /role_briefs\.bull_counter/);
  assert.throws(() => validateMethodology({ ...methodology, text: "" }), /methodology\.text/);
});

test("accepts a structurally valid judgment result", () => {
  const result = {
    protocol_version: "1",
    workflow_version: "1",
    run_id: "r1",
    trade_date: "2026-08-13",
    candidate_hash: "candidate-hash",
    input_hash: "input-hash",
    coarse: [{ ts_code: "000000.SZ", rank: 1, score: 80, reason: "基本面" }],
    deep: [{ ts_code: "000000.SZ", rank: 1, score: 78, analysts: {
      methodology: { stance: "bull", conclusion: "看多", risks: [] },
      sentiment: { stance: "neutral", conclusion: "中性", risks: [] },
      trend: { stance: "bull", conclusion: "看多", risks: [] }
    }}],
    final: [{ ts_code: "000000.SZ", rank: 1, decision: "buy", score: 79, bull_case: "增长", bear_case: "波动", rebuttal: "风险可控", risk_control: "止损" }],
    usage: { input_tokens: 1, output_tokens: 2 }
  };
  assert.doesNotThrow(() => validateJudgmentResult(result, validRequest, "r1"));
});

test("accepts fewer valid batch results than the configured upper limits", () => {
  const batchRequest = {
    ...validRequest,
    mode: "batch" as const,
    limits: { coarse: 2, deep: 1, final: 1 },
    candidates: [
      { ts_code: "000000.SZ", name: "甲" },
      { ts_code: "000001.SZ", name: "乙" },
    ],
    snapshots: [
      { ts_code: "000000.SZ", name: "甲" },
      { ts_code: "000001.SZ", name: "乙" },
    ],
  };
  const result = {
    protocol_version: "1", workflow_version: "1", run_id: "partial", trade_date: batchRequest.trade_date,
    candidate_hash: batchRequest.candidate_hash, input_hash: batchRequest.input_hash,
    coarse: [
      { ts_code: "000000.SZ", rank: 1, score: 80, reason: "有效" },
      { ts_code: "000001.SZ", rank: 2, score: 70, reason: "有效" },
    ],
    deep: [{ ts_code: "000000.SZ", rank: 1, score: 78, analysts: {
      methodology: { stance: "bull", conclusion: "看多", risks: [] },
      sentiment: { stance: "neutral", conclusion: "中性", risks: [] },
      trend: { stance: "bull", conclusion: "看多", risks: [] },
    }}],
    final: [{ ts_code: "000000.SZ", rank: 1, decision: "buy", score: 79, bull_case: "增长", bear_case: "波动", rebuttal: "可控", risk_control: "止损" }],
  };

  assert.doesNotThrow(() => validateJudgmentResult(result, batchRequest, "partial"));
});

test("rejects a result with an invalid subset", () => {
  const result = {
    protocol_version: "1", workflow_version: "1", run_id: "r1", trade_date: "2026-08-13", candidate_hash: "candidate-hash", input_hash: "input-hash",
    coarse: [{ ts_code: "000000.SZ", rank: 1, score: 80, reason: "基本面" }], deep: [], final: [{ ts_code: "000001.SZ", rank: 1, decision: "buy", score: 80, bull_case: "a", bear_case: "b", rebuttal: "c", risk_control: "d" }]
  };
  assert.throws(() => validateJudgmentResult(result, validRequest, "r1"), /subset/);
});
