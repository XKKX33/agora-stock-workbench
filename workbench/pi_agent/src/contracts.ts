import { createHash } from "node:crypto";

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((entry) => canonicalJson(entry)).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

function sha256(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}
export function computeCandidateHash(candidates: Candidate[]): string { return sha256(candidates); }
export function computeInputHash(candidates: Candidate[], snapshots: Snapshot[]): string { return sha256({ candidates, snapshots }); }
export type Mode = "batch" | "single";

// 角色清单与 engine/methodology.py 保持一致：Python 拥有方法论与角色职责，TS 只拥有输出 schema。
export const ANALYST_ROLES = ["methodology", "sentiment", "trend"] as const;
export const DEBATE_ROUNDS = ["bull", "bear", "bull_counter", "risk_chair"] as const;
// 最终决策人 final_pick:逐票风控结束后看完全部结论统一选名单,保证最后一个
// agent 必须给出 N 只——逐票风控各判各的,全判看空时一只都选不出。
export const FINAL_PICK_ROLE = "final_pick" as const;
export const AGENT_ROLES = [...ANALYST_ROLES, ...DEBATE_ROUNDS, FINAL_PICK_ROLE] as const;
export type AnalystRole = (typeof ANALYST_ROLES)[number];
export type DebateRole = (typeof DEBATE_ROUNDS)[number];
export type FinalPickRole = typeof FINAL_PICK_ROLE;

export interface Methodology { text: string; role_briefs: Record<string, string> }

export interface JudgmentRequest {
  protocol_version: string;
  workflow_version: string;
  mode: Mode;
  trade_date: string;
  candidate_hash: string;
  input_hash: string;
  limits: { coarse: number; deep: number; final: number };
  candidates: Candidate[];
  snapshots: Snapshot[];
  model: { provider: string; model: string; reasoning_effort?: string; max_tokens: number };
  methodology: Methodology;
}

export interface Candidate { ts_code: string; name?: string; score?: number; [key: string]: unknown }
export interface Snapshot { ts_code: string; [key: string]: unknown }
export interface Analyst { stance: "bull" | "bear" | "neutral"; conclusion: string; risks: string[] }
export interface DeepJudgment { ts_code: string; rank: number; score: number; analysts: Record<AnalystRole, Analyst> }
export interface FinalJudgment { ts_code: string; rank: number; decision: string; score: number; reason: string; bull_case: string; bear_case: string; rebuttal: string; risk_control: string }
export interface FinalPick { ts_code: string; rank: number; reason: string }
export interface JudgmentResult {
  protocol_version: string; workflow_version: string; run_id: string; trade_date: string; candidate_hash: string; input_hash: string;
  coarse: Array<{ ts_code: string; rank: number; score: number; reason: string }>;
  deep: DeepJudgment[]; final: FinalJudgment[]; picks: FinalPick[]; usage?: { input_tokens?: number; output_tokens?: number };
}

function object(value: unknown): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("request must be an object");
  return value as Record<string, any>;
}
function text(value: unknown, field: string): string { if (typeof value !== "string" || !value.trim()) throw new Error(field); return value; }
function finiteScore(value: unknown, field: string): number { if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 100) throw new Error(field); return value; }
export function validateAnalyst(value: unknown, field: string): Analyst {
  const x = object(value); const stance = x.stance;
  if (stance !== "bull" && stance !== "bear" && stance !== "neutral") throw new Error(`${field}.stance`);
  if (!Array.isArray(x.risks) || x.risks.some((r: unknown) => typeof r !== "string")) throw new Error(`${field}.risks`);
  return { stance, conclusion: text(x.conclusion, `${field}.conclusion`), risks: x.risks };
}

/** 方法论载荷由 Python 装配，这里只校验必填角色齐全，缺任何一个都拒绝执行。 */
export function validateMethodology(value: unknown): Methodology {
  const x = object(value);
  const briefs = object(x.role_briefs);
  const role_briefs: Record<string, string> = {};
  for (const role of AGENT_ROLES) role_briefs[role] = text(briefs[role], `methodology.role_briefs.${role}`);
  return { text: text(x.text, "methodology.text"), role_briefs };
}

export function validateJudgmentRequest(input: unknown): JudgmentRequest {
  const x = object(input); text(x.protocol_version, "protocol_version"); text(x.workflow_version, "workflow_version");
  if (x.mode !== "batch" && x.mode !== "single") throw new Error("mode"); text(x.trade_date, "trade_date"); text(x.candidate_hash, "candidate_hash"); text(x.input_hash, "input_hash");
  const limits = object(x.limits); for (const key of ["coarse", "deep", "final"]) if (!Number.isInteger(limits[key]) || limits[key] < 1) throw new Error(`limits.${key}`);
  if (limits.coarse > 20 || limits.deep > 20 || limits.final > 3) throw new Error("limits exceed Top20 to Top3 contract");
  if (!Array.isArray(x.candidates) || !Array.isArray(x.snapshots)) throw new Error("candidates/snapshots");
  const candidates = x.candidates.map((candidate: unknown) => { const c = object(candidate); const parsed: Candidate = { ...c, ts_code: text(c.ts_code, "candidate.ts_code") }; if (c.score !== undefined) parsed.score = finiteScore(c.score, "candidate.score"); return parsed; });
  const snapshots = x.snapshots.map((snapshot: unknown) => { const s = object(snapshot); return { ...s, ts_code: text(s.ts_code, "snapshot.ts_code") }; });
  if (candidates.length > 20 || candidates.length > limits.coarse || limits.deep > candidates.length || limits.final > limits.deep) throw new Error("limits");
  if (sha256(candidates) !== x.candidate_hash) throw new Error("candidate_hash mismatch");
  if (sha256({ candidates, snapshots }) !== x.input_hash) throw new Error("input_hash mismatch");
  return { protocol_version: x.protocol_version, workflow_version: x.workflow_version, mode: x.mode, trade_date: x.trade_date, candidate_hash: x.candidate_hash, input_hash: x.input_hash, limits: { coarse: limits.coarse, deep: limits.deep, final: limits.final }, candidates, snapshots, model: x.model ?? { provider: "faux", model: "faux", max_tokens: 8192 }, methodology: validateMethodology(x.methodology) };
}

export function validateJudgmentResult(input: unknown, request: JudgmentRequest, runId: string): JudgmentResult {
  const x = object(input); const expected: Record<string, string> = { protocol_version: request.protocol_version, workflow_version: request.workflow_version, trade_date: request.trade_date, candidate_hash: request.candidate_hash, input_hash: request.input_hash };
  for (const key of Object.keys(expected)) if (x[key] !== expected[key]) throw new Error(key);
  if (x.run_id !== runId) throw new Error("run_id");
  if (!Array.isArray(x.coarse) || !Array.isArray(x.deep) || !Array.isArray(x.final)) throw new Error("coarse/deep/final");
  const pool = new Set(request.candidates.map((c) => c.ts_code)); const coarseCodes = new Set<string>();
  const coarse = x.coarse.map((item: unknown, i: number) => { const y = object(item); const code = text(y.ts_code, "coarse.ts_code"); if (!pool.has(code) || coarseCodes.has(code) || y.rank !== i + 1) throw new Error("coarse subset/rank"); coarseCodes.add(code); return { ts_code: code, rank: y.rank, score: finiteScore(y.score, "coarse.score"), reason: text(y.reason, "coarse.reason") }; });
  const deepCodes = new Set<string>(); const deep = x.deep.map((item: unknown, i: number) => { const y = object(item); const code = text(y.ts_code, "deep.ts_code"); if (!coarseCodes.has(code) || deepCodes.has(code) || y.rank !== i + 1) throw new Error("deep subset/rank"); deepCodes.add(code); const analysts = object(y.analysts); const parsed = {} as Record<AnalystRole, Analyst>; for (const role of ANALYST_ROLES) parsed[role] = validateAnalyst(analysts[role], role); return { ts_code: code, rank: y.rank, score: finiteScore(y.score, "deep.score"), analysts: parsed }; });
  const finalCodes = new Set<string>(); const final = x.final.map((item: unknown, i: number) => { const y = object(item); const code = text(y.ts_code, "final.ts_code"); if (!deepCodes.has(code) || finalCodes.has(code) || y.rank !== i + 1) throw new Error("final subset/rank"); finalCodes.add(code); return { ts_code: code, rank: y.rank, decision: text(y.decision, "final.decision"), score: finiteScore(y.score, "final.score"), reason: text(y.reason ?? y.bull_case, "final.reason"), bull_case: text(y.bull_case, "final.bull_case"), bear_case: text(y.bear_case, "final.bear_case"), rebuttal: text(y.rebuttal, "final.rebuttal"), risk_control: text(y.risk_control, "final.risk_control") }; });
  if (request.mode === "batch" && (coarse.length > request.limits.coarse || deep.length > request.limits.deep || final.length > request.limits.final)) throw new Error("batch cardinality exceeds limits");
  const picksRaw: unknown = x.picks;
  const pickedCodes = new Set<string>();
  const picks: FinalPick[] = Array.isArray(picksRaw) ? picksRaw.map((item: unknown, i: number) => { const y = object(item); const code = text(y.ts_code, "picks.ts_code"); if (!finalCodes.has(code) || pickedCodes.has(code) || y.rank !== i + 1) throw new Error("picks subset/rank"); pickedCodes.add(code); return { ts_code: code, rank: y.rank, reason: text(y.reason, "picks.reason") }; }) : [];
  if (request.mode === "single" && (coarse.length > 1 || deep.length > 1 || final.length > 1)) throw new Error("single cardinality exceeds limits");
  return { protocol_version: x.protocol_version, workflow_version: x.workflow_version, run_id: runId, trade_date: x.trade_date, candidate_hash: x.candidate_hash, input_hash: x.input_hash, coarse, deep, final, picks, usage: x.usage };
}
