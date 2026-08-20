import { validateAnalyst, validateJudgmentRequest, validateJudgmentResult, type JudgmentResult } from "./contracts.js";
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

export async function runWorkflow(runId: string, rawRequest: unknown, provider: RoleProvider, emit: EventSink, signal: AbortSignal): Promise<JudgmentResult> {
  const request = validateJudgmentRequest(rawRequest);
  const usage = { input_tokens: 0, output_tokens: 0 };
  const call = async (role: string, payload: Record<string, unknown>): Promise<Record<string, any>> => {
    const response = await provider.complete(role, JSON.stringify(payload), signal);
    if (!response.text.trim() || response.finishReason === "length") throw new Error(`${role} returned incomplete output`);
    usage.input_tokens += response.inputTokens ?? 0;
    usage.output_tokens += response.outputTokens ?? 0;
    const parsed = parseJsonEnvelope(response.text);
    if (parsed) return parsed;
    throw new Error(`${role} returned invalid JSON`);
  };
  emit({ type: "run.started", data: { run_id: runId, input_hash: request.input_hash } });
  const coarse = request.candidates.map((candidate, index) => ({ ts_code: candidate.ts_code, rank: index + 1, score: Number(candidate.score ?? 50), reason: "方法论候选" }));
  for (const item of coarse) if (!Number.isFinite(item.score) || item.score < 0 || item.score > 100) throw new Error("coarse returned invalid score");
  const deep: JudgmentResult["deep"] = [];
  for (const item of coarse) {
    const snapshot = request.snapshots.find((candidate) => candidate.ts_code === item.ts_code);
    if (!snapshot) {
      emit({ type: "message.failed", data: { ts_code: item.ts_code, error: "snapshot missing", input_hash: request.input_hash } });
      continue;
    }
    const analysts: Record<string, any> = {};
    try {
      for (const role of ["methodology", "sentiment", "technical"]) {
        analysts[role] = validateAnalyst(
          await call(role, { ...snapshot, score: item.score }),
          role,
        );
        emit({ type: "message.completed", data: { role, ts_code: item.ts_code, input_hash: request.input_hash } });
      }
    } catch (error) {
      emit({ type: "message.failed", data: { ts_code: item.ts_code, error: error instanceof Error ? error.message : String(error), input_hash: request.input_hash } });
      continue;
    }
    deep.push({ ts_code: item.ts_code, rank: deep.length + 1, score: item.score, analysts: analysts as JudgmentResult["deep"][number]["analysts"] });
  }
  const debate = await call("debate", { candidates: deep, limit: request.limits.final });
  if (!Array.isArray(debate.selected)) throw new Error("debate selected must be an array");
  const deepByCode: Record<string, JudgmentResult["deep"][number]> = Object.fromEntries(deep.map((item) => [item.ts_code, item]));
  const final = debate.selected.slice(0, request.limits.final).map((entry: Record<string, any>, index: number) => {
    const item = deepByCode[String(entry.ts_code)];
    if (!item) throw new Error("debate selected unknown candidate");
    const reason = typeof entry.reason === "string" && entry.reason.trim() ? entry.reason.trim() : "模型未提供入选理由";
    emit({ type: "message.completed", data: { role: "debate", ts_code: item.ts_code, input_hash: request.input_hash } });
    return { ts_code: item.ts_code, rank: index + 1, decision: String(entry.decision ?? "selected"), score: Number(entry.score ?? item.score), reason, bull_case: reason, bear_case: String(entry.bear_case ?? "未提供"), rebuttal: String(entry.rebuttal ?? "未提供"), risk_control: String(entry.risk_control ?? "未提供") };
  });
  const output = { protocol_version: request.protocol_version, workflow_version: request.workflow_version, run_id: runId, trade_date: request.trade_date, candidate_hash: request.candidate_hash, input_hash: request.input_hash, coarse, deep, final, usage };
  return validateJudgmentResult(output, request, runId);
}
