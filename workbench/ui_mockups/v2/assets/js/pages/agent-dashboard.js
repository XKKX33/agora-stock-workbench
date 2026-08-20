
import { clearError, getWorkContext, initShell, setLoading, setStatus, setWorkContext, showError, workContextParams } from "/assets/js/app-shell.js";
import { query } from "/assets/js/api.js";
import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("agent-dashboard");
const ROLE_LABELS = { methodology: "方法论", sentiment: "舆情", trend: "走势", bull: "多方", bear: "空方", bull_counter: "多方反驳", risk_chair: "风控" };
const STAGE_LABELS = { analysis: "分析", debate: "辩论", risk: "风控", done: "完成" };
const HORIZON_LABELS = { t1_close: "T+1 收盘", t2_open: "T+2 开盘", t3_open: "T+3 开盘", t4_open: "T+4 开盘", t5_open: "T+5 开盘", t6_open: "T+6 开盘", t7_open: "T+7 开盘", t8_open: "T+8 开盘", t9_open: "T+9 开盘", t10_open: "T+10 开盘" };
const TERMINAL_EVENTS = new Set(["run.completed", "run.failed"]);
const urlRunId = new URLSearchParams(window.location.search).get("run_id") || "";
let selectedJobId = "";
let lastSeq = 0;
let eventSource = null;
let reconnectTimer = null;
const el = (id) => document.getElementById(id);
const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
const parseJson = (value, fallback = {}) => { if (value && typeof value === "object") return value; try { return JSON.parse(value || ""); } catch { return fallback; } };
const eventContent = (event) => parseJson(event?.content_json ?? event?.content, {});
const eventCitations = (event) => parseJson(event?.citations_json ?? event?.citations, []);
const eventSeq = (event) => Number(event?.seq ?? event?.id ?? 0) || 0;
function roleLabel(role) { return ROLE_LABELS[role] || text(role, "公开消息"); }
function stageLabel(stage) { return STAGE_LABELS[stage] || text(stage, "阶段未标注"); }
function displayContent(content) { if (content === null || content === undefined || content === "") return "暂无公开内容"; if (typeof content === "string") return content; const preferred = content.summary || content.thesis || content.verdict || content.message || content.content; return preferred ? String(preferred) : JSON.stringify(content, null, 2); }
function citationHtml(citations) { const items = Array.isArray(citations) ? citations : []; if (!items.length) return ""; return `<div class="event-citations"><span>引用</span>${items.slice(0, 8).map((item) => { const label = typeof item === "string" ? item : item?.title || item?.source || item?.url || "资料"; return `<span class="tag muted">${escapeHtml(label)}</span>`; }).join("")}</div>`; }

export function renderEvent(event, { pending = false } = {}) {
  const kind = event?.event_type || event?.type || "message.completed";
  const status = pending || kind === "message.delta" ? "流式" : event?.status || "已完成";
  const stateClass = pending || kind === "message.delta" ? "active" : event?.status === "failed" ? "bad" : "good";
  return `<article class="event-item ${pending ? "event-pending" : ""}" data-seq="${escapeHtml(eventSeq(event))}"><div class="event-meta"><span class="mono">#${escapeHtml(eventSeq(event) || "—")}</span><strong>${escapeHtml(roleLabel(event?.role))}</strong><span>${escapeHtml(stageLabel(event?.stage))}</span>${statusTag(status, stateClass)}</div><div class="event-content">${escapeHtml(kind === "run.failed" ? displayContent(eventContent(event)) : displayContent(eventContent(event)))}</div>${citationHtml(eventCitations(event))}</article>`;
}

function timelineEvents(events) {
  const latestByRole = new Map();
  const other = [];
  events.forEach((event) => {
    const kind = event?.event_type || event?.type;
    if (!event?.role || !["message.delta", "message.completed"].includes(kind)) { other.push(event); return; }
    const current = latestByRole.get(event.role);
    if (!current || eventSeq(event) >= eventSeq(current) || kind === "message.completed") latestByRole.set(event.role, event);
  });
  return [...other, ...latestByRole.values()];
}

function renderStatus(job) {
  const state = text(job?.status, "unknown");
  const kind = state === "succeeded" ? "good" : state === "failed" ? "bad" : state === "running" ? "active" : "pending";
  const tag = el("batch-status-tag");
  if (tag) { tag.className = `tag ${kind}`; tag.textContent = { succeeded: "已完成", failed: "失败", running: "运行中", queued: "排队中" }[state] || state; }
  const note = el("batch-status-note");
  if (note) note.textContent = job ? `批次 ${text(job.job_id || job.task_id || job.run_id)} · ${formatDate(job.created_at)}` : "选择批次后加载详情";
  const host = el("batch-status");
  if (!host) return;
  const progress = job?.progress || {};
  const params = job?.params || {};
  if (job) setWorkContext({ run_id: job.run_id || job.job_id || job.task_id, strategy: job.strategy, as_of: job.as_of || job.signal_date, data_cutoff: job.data_cutoff || job.data_cutoff_at, availability: job.availability, missing_reason: job.missing_reason });
  host.innerHTML = job ? [["信号日", formatDate(job.as_of || job.signal_date)], ["当前阶段", text(progress.stage || job.stage)], ["进度", progress.percent == null ? "—" : `${formatNumber(progress.percent, 0)}%`], ["候选 / 深度 / 最终", [params.candidates, params.depth, params.final].map((v) => v == null ? "—" : formatNumber(v, 0)).join(" / ")]].map(([label, value]) => `<div class="dashboard-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("") : `<div class="empty-state">暂无批次数据</div>`;
}
function renderTimeline(events) { const host = el("agent-event-timeline"); if (!host) return; const sorted = [...timelineEvents(events)].sort((a, b) => eventSeq(a) - eventSeq(b)); host.innerHTML = sorted.length ? sorted.map((item) => renderEvent(item, { pending: (item?.event_type || item?.type) === "message.delta" })).join("") : `<div class="empty-state">暂无公开通话</div>`; }

export function renderDebateMatrix(events = []) {
  const host = el("debate-matrix");
  const latest = new Map();
  events.forEach((event) => { if (event?.role) latest.set(event.role, event); });
  if (host) host.innerHTML = ["methodology", "sentiment", "trend", "bull", "bear", "bull_counter"].map((role) => { const event = latest.get(role); return `<section class="debate-cell"><div class="debate-cell-head"><strong>${escapeHtml(roleLabel(role))}</strong>${event ? statusTag(event.event_type === "message.delta" ? "流式" : "已收到", event.event_type === "message.delta" ? "active" : "good") : statusTag("待发布", "muted")}</div><p>${escapeHtml(event ? displayContent(eventContent(event)) : "尚未收到公开消息")}</p>${event ? citationHtml(eventCitations(event)) : ""}</section>`; }).join("");
  const risk = latest.get("risk_chair");
  const riskHost = el("risk-chair");
  if (riskHost) riskHost.innerHTML = risk ? `<div class="risk-verdict"><strong>${escapeHtml(displayContent(eventContent(risk)))}</strong>${citationHtml(eventCitations(risk))}</div>` : `<div class="empty-state">暂无风控结论</div>`;
}

export function renderReturnCards(summary) {
  const host = el("return-cards");
  if (!host) return;
  const groups = summary?.groups && typeof summary.groups === "object" ? Object.entries(summary.groups) : [["", summary]];
  const cards = groups.flatMap(([groupName, group]) => Object.entries(HORIZON_LABELS).map(([horizon, label]) => { const item = group?.[horizon] || null; const available = item?.available === true || item?.measurable_count > 0; const portfolio = available ? formatPercent(item?.portfolio_gross_return ?? item?.portfolio_return) : "暂无可测数据"; const coverage = item?.coverage == null ? "覆盖率 —" : `覆盖率 ${formatPercent(item.coverage)}`; const reason = available ? `${item?.filled_count ?? item?.measurable_count ?? 0} 个可测槽位` : text(Object.keys(item?.status_distribution || {})[0], "未来未到或缺少行情"); const groupLabel = groupName ? { rule: "规则", ai: "AI", hybrid: "混合", benchmark: "基准" }[groupName] || groupName : "组合"; return `<article class="return-card ${available ? "available" : "unavailable"}"><div class="return-card-head"><strong>${escapeHtml(label)}</strong>${statusTag(available ? "可测" : "不可用", available ? "good" : "pending")}</div><div class="return-group">${escapeHtml(groupLabel)}</div><div class="return-value">${escapeHtml(portfolio)}</div><div class="return-meta"><span>${escapeHtml(coverage)}</span><span>${escapeHtml(reason)}</span></div></article>`; }));
  host.innerHTML = cards.length ? cards.join("") : `<div class="empty-state">暂无收益数据</div>`;
}

function closeEventStream() { if (eventSource) { eventSource.close(); eventSource = null; } if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; } }
export function connectEventStream(runId, { onEvent, onState } = {}) {
  closeEventStream(); selectedJobId = runId || selectedJobId;
  if (!selectedJobId || typeof EventSource === "undefined") { onState?.("fallback"); return null; }
  eventSource = new EventSource(`/api/agents/jobs/${encodeURIComponent(selectedJobId)}/stream?after_seq=${encodeURIComponent(lastSeq)}`);
  onState?.("connecting");
  const handleMessage = (message) => { let event; try { event = JSON.parse(message.data); } catch { return; } const seq = eventSeq(event); if (seq > lastSeq) lastSeq = seq; onEvent?.(event); if (TERMINAL_EVENTS.has(event.event_type || event.type || message.type)) { onState?.("closed"); closeEventStream(); } };
  eventSource.onopen = () => onState?.("connected");
  eventSource.onmessage = handleMessage;
  ["run.started", "stage.started", "message.delta", "message.completed", "stage.completed", "run.completed", "run.failed"].forEach((name) => eventSource.addEventListener(name, handleMessage));
  eventSource.addEventListener("heartbeat", () => onState?.("connected"));
  eventSource.onerror = () => { onState?.("reconnecting"); closeEventStream(); reconnectTimer = setTimeout(() => connectEventStream(selectedJobId, { onEvent, onState }), 2000); };
  return eventSource;
}
async function loadJobs() {
  const payload = await query("/api/agents/jobs", { ...workContextParams(), limit: 100 });
  const jobs = payload?.items || [];
  const select = el("agent-job-select");
  if (select) {
    select.innerHTML = jobs.length ? jobs.map((job) => { const id = job.job_id || job.task_id || job.run_id; return `<option value="${escapeHtml(id)}">${escapeHtml(formatDate(job.as_of))} · ${escapeHtml(id)} · ${escapeHtml(text(job.status, "未知"))}</option>`; }).join("") : `<option value="">暂无研判批次</option>`;
  }
  return jobs;
}
async function loadReturns(runId) {
  const summary = await query("/api/returns/summary", { ...workContextParams(), run_id: runId });
  await query("/api/returns", { ...workContextParams(), run_id: runId });
  renderReturnCards(summary);
  const note = el("returns-run-note");
  if (note) note.textContent = runId ? `run ${runId}` : "全部批次";
}
async function loadDashboard(runId) {
  if (!runId) { renderStatus(null); renderTimeline([]); renderDebateMatrix([]); renderReturnCards(null); return; }
  setLoading(true); clearError(); closeEventStream(); lastSeq = 0;
  try { const [job, replay] = await Promise.all([query(`/api/agents/jobs/${encodeURIComponent(runId)}`), query(`/api/agents/jobs/${encodeURIComponent(runId)}/events`, { after_seq: 0, limit: 500 })]); renderStatus(job); const events = replay?.items || []; lastSeq = Number(replay?.next_seq || events.reduce((max, event) => Math.max(max, eventSeq(event)), 0)) || 0; renderTimeline(events); renderDebateMatrix(events); await loadReturns(runId); const streamState = (state) => { const host = el("stream-status"); if (host) { host.className = `tag ${state === "connected" ? "good" : state === "reconnecting" ? "pending" : "muted"}`; host.textContent = { connecting: "连接中", connected: "实时连接", reconnecting: "重连中", fallback: "请手动刷新", closed: "已结束" }[state] || state; } }; if (job.status === "running" || job.status === "queued") connectEventStream(runId, { onEvent: (event) => { renderTimeline([event]); renderDebateMatrix([event]); }, onState: streamState }); else streamState("closed");
  } finally { setLoading(false); }
}
async function refresh() { try { const jobs = await loadJobs(); const select = el("agent-job-select"); const contextRunId = getWorkContext().run_id; const contextJob = jobs.some((job) => (job.job_id || job.task_id || job.run_id) === contextRunId) ? contextRunId : ""; selectedJobId = urlRunId || selectedJobId || contextJob || select?.value || jobs[0]?.job_id || jobs[0]?.task_id || jobs[0]?.run_id || ""; if (selectedJobId) setWorkContext({ run_id: selectedJobId }); if (select && selectedJobId) select.value = selectedJobId; await loadDashboard(selectedJobId); setStatus("Agent 看板已更新", "ready"); } catch (error) { showError(error); } }
initShell("agent-dashboard"); el("agent-dashboard-refresh")?.addEventListener("click", refresh); el("agent-job-select")?.addEventListener("change", (event) => { selectedJobId = event.target.value; setWorkContext({ run_id: selectedJobId }); loadDashboard(selectedJobId); }); refresh();
