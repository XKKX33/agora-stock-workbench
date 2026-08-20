import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, setWorkContext, showError, workContextParams } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, statusTag } from "/assets/js/format.js";

initShell("foundry");

const FALLBACK_STEPS = [
  ["preflight", "配置预检"], ["calendar", "交易日历"], ["market_data", "市场数据"],
  ["backfill_returns", "历史收益"], ["integrity", "完整性"], ["scan", "规则扫描"],
  ["collect_news", "舆情采集"], ["agents", "Agent 研判"], ["persist_experiment", "实验落库"],
];
// 步骤契约来自 GET /api/pipelines/workflow；取不到才退回这份内置顺序。
let STEPS = FALLBACK_STEPS;
let STEP_KEYS = new Map();

const STATE_LABELS = { waiting: "等待", running: "运行", success: "成功", failed: "失败", unavailable: "未执行" };
const TASK_LABELS = { queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败" };
const GATE_LABELS = {
  ready: "可以运行",
  calendar_missing: "交易日历缺失",
  calendar_stale: "交易日历过期",
  before_run_after: "还没到运行时间",
  not_trading_day: "当天不是交易日",
};
const ENTRY_LABELS = { filled: ["已成交", "good"], entry_unavailable: ["无法成交", "bad"], pending_entry: ["待成交", "pending"] };
const GROUPS = [["rule", "规则"], ["ai", "AI"], ["hybrid", "混合"], ["benchmark", "基准"]];
// 步骤 data 里的字段名 -> 中文。没收录的键按原名显示，不隐藏。
const DATA_LABELS = {
  strategy: "策略", model: "模型", online: "在线模式", as_of: "信号日",
  calendar_rows: "日历行数", confirmed_rows: "已确认行数", visible_as_of: "可见信号日",
  base_session: "基准场次", delay_sessions: "延迟场次", hidden_count: "隐藏天数",
  snapshot_count: "快照股票数", candidate_count: "候选数", data_cutoff_at: "数据截止",
  data_quality: "数据质量", ingest_as_of: "摄取日期",
  required_limit_dates: "待补涨跌停日", daily_limit_rows: "涨跌停行数", updated: "更新条数",
  filled: "已成交", pending: "待成交", unavailable: "无法成交", return_filled: "回填收益条数",
  context_count: "上下文条数", run_id: "扫描批次", scored_count: "完成打分",
  passed_count: "通过门槛", rule_final_count: "规则最终入选", candidate_hash: "候选指纹",
  sources: "舆情源", items: "舆情条数", linked: "关联股票数",
  candidates: "送审候选", depth: "研判深度", final_count: "最终入选", group_counts: "四组数量",
};

let activeJob = null;      // { id, kind } 正在轮询的任务
let pollTimer = null;
let currentTask = null;    // 步骤条当前展示的一键任务
let selectedStep = null;
let historyKind = "one_click_pipeline";
let historyItems = [];
let groupTab = "rule";
let groupAsOf = null;
// 基准组是整个候选池,一页装不下;接口 per_page 上限 200,页数由接口 total 决定
const GROUP_PAGE_SIZE = 200;
let groupPage = 1;

// 换批次就回到第一页:上一批次的页码对新批次没有意义
function setGroupAsOf(value) {
  const next = value || null;
  if (next !== groupAsOf) groupPage = 1;
  groupAsOf = next;
}

function stepLabel(name) {
  const hit = STEPS.find(([key]) => key === name);
  return hit ? hit[1] : name;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function duration(task) {
  const from = task?.started_at || task?.created_at;
  const to = task?.finished_at || task?.heartbeat_at;
  if (!from || !to) return "—";
  const seconds = (new Date(to).getTime() - new Date(from).getTime()) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function dataText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return formatNumber(value, 4);
  if (Array.isArray(value)) {
    if (!value.length) return "无";
    const head = value.slice(0, 6).map((item) => (typeof item === "object" ? JSON.stringify(item) : String(item)));
    return value.length > 6 ? `${head.join("、")} 等 ${value.length} 项` : head.join("、");
  }
  if (typeof value === "object") {
    const parts = Object.entries(value).map(([key, item]) => `${DATA_LABELS[key] || key}=${typeof item === "object" ? JSON.stringify(item) : item}`);
    return parts.length ? parts.join(" · ") : "无";
  }
  return String(value);
}

// ------------------------------------------------------------------ 步骤条
function stepState(task, stepName, index) {
  const completed = new Map((task?.result?.steps || []).map((item) => [item.name, item]));
  const failedStep = task?.error?.failed_step;
  const failedIndex = failedStep ? STEPS.findIndex(([name]) => name === failedStep) : -1;
  if (failedIndex >= 0 && index > failedIndex) return "unavailable";
  if (task?.status === "failed" && failedStep === stepName) return "failed";
  if (completed.has(stepName)) {
    const status = completed.get(stepName).status;
    if (status === "unavailable" || status === "skipped") return "unavailable";
    return "success";
  }
  if (task?.status === "running" && index === completed.size) return "running";
  return "waiting";
}

function renderPipeline(task) {
  currentTask = task;
  const host = document.querySelector("#pipeline-steps");
  host.innerHTML = STEPS.map(([name, label], index) => {
    const state = stepState(task, name, index);
    const selected = selectedStep === name ? " selected" : "";
    return `<div class="pipeline-step ${state}${selected}" data-step="${escapeHtml(name)}" role="button" tabindex="0" title="点开看这一步做了什么">
      <span class="step-index mono">${String(index + 1).padStart(2, "0")}</span>
      <span class="step-name">${escapeHtml(label)}</span>
      <span class="step-state">${STATE_LABELS[state]}</span>
    </div>`;
  }).join("");

  const status = task?.status || "idle";
  const asOf = task?.result?.as_of || task?.trade_date;
  const groupCounts = task?.result?.group_counts || {};
  const cutoff = task?.result?.data_cutoff_at;
  document.querySelector("#pipeline-state").textContent = status === "idle"
    ? "等待任务"
    : `${TASK_LABELS[status] || status}${asOf ? ` · 信号日 ${formatDate(asOf)}` : ""}${task?.strategy ? ` · ${task.strategy}` : ""}${cutoff ? ` · 数据截止 ${formatTime(cutoff)}` : ""}`;
  document.querySelector("#pipeline-meta").textContent = task
    ? `批次 ${String(task.task_id || task.job_id || "").slice(0, 8)} · 提交 ${formatTime(task.created_at)} · 耗时 ${duration(task)}`
    : "尚未运行";

  const failure = document.querySelector("#pipeline-error");
  if (task?.status === "failed" && task?.error) {
    const failedStep = task.error.failed_step;
    const done = (task.error.completed_steps || []).length;
    failure.textContent = `失败于「${failedStep ? stepLabel(failedStep) : "未知步骤"}」（已完成 ${done}/${STEPS.length} 步）：${task.error.message || "无错误信息"}`;
    failure.hidden = false;
  } else {
    failure.textContent = "";
    failure.hidden = true;
  }

  renderStepDetail();
  const running = ["queued", "running"].includes(status);
  document.querySelector("#one-click").disabled = running;
  document.querySelector("#backfill-run").disabled = running;
  if (Object.keys(groupCounts).length) {
    document.querySelector("#group-meta").textContent = `信号日 ${formatDate(asOf)} · 规则 ${groupCounts.rule || 0} / AI ${groupCounts.ai || 0} / 混合 ${groupCounts.hybrid || 0} / 基准 ${groupCounts.benchmark || 0}${cutoff ? ` · 数据截止 ${formatTime(cutoff)}` : ""}`;
  }
}

function renderStepDetail() {
  const host = document.querySelector("#step-detail");
  if (!selectedStep) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  const step = (currentTask?.result?.steps || []).find((item) => item.name === selectedStep);
  const index = STEPS.findIndex(([name]) => name === selectedStep);
  const state = stepState(currentTask, selectedStep, index);
  const keys = STEP_KEYS.get(selectedStep) || [];
  const data = step?.data || {};
  const ordered = [...keys.filter((key) => key in data), ...Object.keys(data).filter((key) => !keys.includes(key))];
  const rows = ordered.map((key) => `<div class="kv"><span>${escapeHtml(DATA_LABELS[key] || key)}</span><span>${escapeHtml(dataText(data[key]))}</span></div>`).join("");
  const failureDetail = state === "failed" && currentTask?.error?.message
    ? `失败原因：${escapeHtml(currentTask.error.message)}`
    : null;
  host.hidden = false;
  host.innerHTML = `<div class="step-detail-head">
      <strong>${escapeHtml(String(index + 1).padStart(2, "0"))} · ${escapeHtml(stepLabel(selectedStep))}</strong>
      ${statusTag(STATE_LABELS[state], state === "success" ? "good" : state === "failed" ? "bad" : state === "running" ? "active" : "muted")}
    </div>
    ${step?.detail ? `<p>${escapeHtml(step.detail)}</p>` : failureDetail ? `<p>${failureDetail}</p>` : `<p>${state === "waiting" ? "这一步还没开始。" : state === "unavailable" ? "这一步没有执行。" : "步骤说明暂不可用。"}</p>`}
    ${rows || ""}`;
}

// -------------------------------------------------------------- 调度与闸门
function renderSchedule(status) {
  const rows = [
    ["自动调度", status.enabled ? "已开启" : "已关闭"],
    ["调度线程", status.enabled ? (status.running ? "正在轮询" : "没有在跑（故障）") : "未启动"],
    ["触发时间", `${status.run_after} 之后`],
    ["日历口径", status.exchange],
    ["默认策略", status.strategy],
    ["数据来源", status.online ? "在线拉取" : "仅用本地库"],
    ["轮询间隔", `${formatNumber(status.tick_seconds, 0)} 秒`],
    ["上次轮询", `${formatTime(status.last_tick_at)}${status.last_tick_detail ? ` · ${status.last_tick_detail}` : ""}`],
  ];
  document.querySelector("#schedule-grid").innerHTML = rows
    .map(([key, value]) => `<div class="kv"><span>${escapeHtml(key)}</span><span>${escapeHtml(value)}</span></div>`)
    .join("");
  document.querySelector("#schedule-state").textContent = status.enabled
    ? (status.running ? "自动调度正常运行" : "配置已开启，但调度线程没有在跑")
    : "自动调度已关闭，只能手动运行";

  const gate = status.gate || {};
  const note = document.querySelector("#gate-note");
  note.classList.toggle("blocked", !gate.should_run);
  note.innerHTML = `<strong>闸门结论：${gate.should_run ? "可以运行" : "暂不运行"}</strong>（${escapeHtml(GATE_LABELS[gate.reason] || gate.reason || "未知")}）`
    + `${gate.trade_date ? ` · 目标交易日 ${escapeHtml(formatDate(gate.trade_date))}` : ""}`
    + `${gate.detail ? `<br>${escapeHtml(gate.detail)}` : ""}`
    + `${status.last_error ? `<br>上次自动触发失败：${escapeHtml(status.last_error)}` : ""}`;
}

// ------------------------------------------------------------------ 四组结果
function renderGroupTabs(counts = {}) {
  document.querySelector("#group-tabs").innerHTML = GROUPS
    .map(([key, label]) => `<button class="tab-btn${groupTab === key ? " active" : ""}" type="button" data-group="${key}">${label}${counts[key] === undefined ? "" : `（${counts[key]}）`}</button>`)
    .join("");
}

function groupRowHtml(item) {
  const score = { rule: item.rule_score, ai: item.ai_score, hybrid: item.hybrid_score }[groupTab];
  const [entryLabel, entryKind] = ENTRY_LABELS[item.entry_status] || ["未知", "muted"];
  return `<tr>
    <td class="mono">${escapeHtml(formatNumber(item.rank, 0))}</td>
    <td><strong>${escapeHtml(item.name || "—")}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td>
    <td>${escapeHtml(item.industry || "—")}</td>
    <td class="mono accent">${escapeHtml(groupTab === "benchmark" ? "—" : formatNumber(score, 4))}</td>
    <td class="mono">${escapeHtml(formatNumber(item.entry_price, 2))}</td>
    <td>${statusTag(entryLabel, entryKind)}</td>
  </tr>`;
}

function renderGroupPagination(total) {
  const totalPages = Math.max(1, Math.ceil(Number(total || 0) / GROUP_PAGE_SIZE));
  groupPage = Math.min(Math.max(1, groupPage), totalPages);
  document.querySelector("#group-page-label").textContent = `第 ${groupPage} / ${totalPages} 页`;
  document.querySelector("#group-prev").disabled = groupPage <= 1;
  document.querySelector("#group-next").disabled = groupPage >= totalPages;
}

async function loadGroups() {
  const host = document.querySelector("#group-rows");
  if (!groupAsOf) {
    renderGroupTabs();
    host.innerHTML = emptyRow("暂无实验批次", 6);
    document.querySelector("#group-meta").textContent = "等待批次";
    renderGroupPagination(0);
    return;
  }
  const data = await query("/api/experiments", {
    ...workContextParams(),
    as_of: groupAsOf,
    group: groupTab,
    page: groupPage,
    per_page: GROUP_PAGE_SIZE,
  });
  renderGroupTabs(currentTask?.result?.group_counts || {});
  host.innerHTML = data.items?.length
    ? data.items.map(groupRowHtml).join("")
    : emptyRow(`${GROUPS.find(([key]) => key === groupTab)?.[1] || groupTab}组在 ${formatDate(groupAsOf)} 没有记录`, 6);
  // 表格只显示当前页,所以总条数与数据截止时间一律写明,不让页签数字和表格行数对不上
  const cutoff = data.items?.[0]?.data_cutoff_at;
  document.querySelector("#group-meta").textContent =
    `信号日 ${formatDate(groupAsOf)} · 当前组 ${formatNumber(data.total, 0)} 条${cutoff ? ` · 数据截止 ${formatTime(cutoff)}` : ""}`;
  renderGroupPagination(data.total);
}

// ------------------------------------------------------------------ 任务历史
function historySummary(task) {
  if (task.kind === "one_click_backfill") {
    const result = task.result || {};
    const dates = result.dates || [];
    if (task.status === "succeeded") return `补齐 ${(result.completed || []).length} 天，复用 ${(result.reused || []).length} 天`;
    if (task.status === "failed") return `停在 ${formatDate(task.error?.failed_date || result.failed_date)} · ${task.error?.message || "无错误信息"}`;
    if (result.current_date) return `正在跑 ${formatDate(result.current_date)}（共 ${dates.length} 天）`;
    return `待补 ${dates.length} 天`;
  }
  if (task.status === "succeeded") {
    const counts = task.result?.group_counts || {};
    return `规则 ${counts.rule || 0} / AI ${counts.ai || 0} / 混合 ${counts.hybrid || 0} / 基准 ${counts.benchmark || 0}`;
  }
  if (task.status === "failed") {
    return `失败于「${task.error?.failed_step ? stepLabel(task.error.failed_step) : "未知步骤"}」· ${task.error?.message || "无错误信息"}`;
  }
  const done = (task.result?.steps || []).length;
  return `第 ${Math.min(done + 1, STEPS.length)}/${STEPS.length} 步 ${stepLabel(task.result?.current_step || STEPS[Math.min(done, STEPS.length - 1)][0])}`;
}

function renderHistory() {
  document.querySelectorAll("#history-tabs .tab-btn").forEach((node) => {
    node.classList.toggle("active", node.dataset.kind === historyKind);
  });
  const host = document.querySelector("#history-rows");
  if (!historyItems.length) {
    host.innerHTML = emptyRow(historyKind === "one_click_backfill" ? "还没有补齐任务" : "还没有一键全流程任务", 6);
    return;
  }
  const kindKind = { succeeded: "good", failed: "bad", running: "active", queued: "pending" };
  host.innerHTML = historyItems.map((task) => `<tr data-job="${escapeHtml(task.job_id || task.task_id)}" style="cursor:pointer">
    <td class="mono">${escapeHtml(formatTime(task.created_at))}</td>
    <td class="mono">${escapeHtml(formatDate(task.trade_date))}</td>
    <td>${escapeHtml(task.strategy || "—")}</td>
    <td>${statusTag(TASK_LABELS[task.status] || task.status, kindKind[task.status] || "muted")}</td>
    <td class="mono">${escapeHtml(duration(task))}</td>
    <td class="muted">${escapeHtml(historySummary(task))}</td>
  </tr>`).join("");
}

async function loadHistory() {
  const data = await query("/api/pipelines", { limit: 10, kind: historyKind });
  historyItems = data.items || [];
  renderHistory();
}

// ------------------------------------------------------------------ 数据加载
async function loadWorkflow() {
  const workflow = await request("/api/pipelines/workflow");
  const steps = (workflow.steps || []).map((item) => [item.name, item.display_label]);
  STEPS = steps.length ? steps : FALLBACK_STEPS;
  STEP_KEYS = new Map((workflow.steps || []).map((item) => [item.name, item.output_keys || []]));
}

async function loadLatest() {
  const data = await query("/api/pipelines", { limit: 1, kind: "one_click_pipeline" });
  const latest = data.items?.[0] || null;
  if (latest) setWorkContext({ run_id: latest.result?.run_id || latest.run_id, strategy: latest.strategy || latest.result?.strategy, as_of: latest.result?.as_of || latest.trade_date, data_cutoff: latest.result?.data_cutoff_at, availability: latest.result?.availability, missing_reason: latest.result?.missing_reason });
  renderPipeline(latest);
  setGroupAsOf(latest?.result?.as_of);
  if (["queued", "running"].includes(latest?.status)) activeJob = { id: latest.job_id, kind: latest.kind };
}

async function loadScan() {
  const [overview, stocks] = await Promise.all([
    request("/api/overview"),
    query("/api/stocks", { per_page: 200, sort: "rank" }),
  ]);
  const scan = overview.latest_scan;
  if (!scan) {
    document.querySelectorAll("[data-funnel]").forEach((node) => { node.textContent = "—"; });
    document.querySelector("#selected-rows").innerHTML = emptyRow("暂无规则扫描批次", 4);
    document.querySelector("#rejected-rows").innerHTML = emptyRow("暂无规则扫描批次", 4);
    return;
  }
  const values = [scan.candidate_count, scan.scored_count, scan.passed_count, scan.final_count];
  document.querySelectorAll("[data-funnel]").forEach((node, index) => { node.textContent = formatNumber(values[index], 0); });
  document.querySelector("#selected-rows").innerHTML = stocks.items.filter((item) => item.selected).map(rowHtml).join("") || emptyRow("暂无入选股票", 4);
  document.querySelector("#rejected-rows").innerHTML = stocks.items.filter((item) => !item.passed).slice(0, 20).map((item) => `
    <tr><td><strong>${escapeHtml(item.name)}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td><td>${escapeHtml(item.industry)}</td><td>${statusTag("未通过", "bad")}</td><td class="muted">${escapeHtml((item.gate_reasons || []).join("；") || "门槛未通过")}</td></tr>`).join("") || emptyRow("没有未通过的候选", 4);
}

async function load() {
  clearError();
  setLoading(true);
  try {
    const status = await request("/api/pipelines/status");
    renderSchedule(status);
    await loadWorkflow();
    await loadLatest();
    await Promise.all([loadHistory(), loadGroups(), loadScan()]);
    setStatus("流程状态已更新", "ready");
    schedulePoll();
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

// ------------------------------------------------------------------ 触发
function optionalOnline(value) {
  return value === "" ? undefined : value === "true";
}

function openAgentDashboard() {
  return window.open("about:blank", "hermes-agent-dashboard");
}

function handoffAgentDashboard(popup, job) {
  const runId = String(job.job_id || job.task_id || "");
  if (!runId) return;
  const context = setWorkContext({
    run_id: runId,
    strategy: job.strategy,
    as_of: job.trade_date,
  });
  const target = `p13_agent_dashboard.html?run_id=${encodeURIComponent(runId)}`;
  if (popup) {
    popup.localStorage.setItem("hermes.work-context", JSON.stringify(context));
    popup.location.replace(target);
    popup.focus();
    return;
  }
  const note = document.querySelector("#trigger-result");
  if (note) {
    note.insertAdjacentHTML(
      "beforeend",
      `<br>popup 被浏览器拦截。<a href="${target}" target="hermes-agent-dashboard">打开 Agent 看板</a>`
    );
  }
}

async function startOneClick(event) {
  event?.preventDefault();
  clearError();
  const button = document.querySelector("#one-click");
  const note = document.querySelector("#trigger-result");
  button.disabled = true;
  const body = {
    trade_date: document.querySelector("#trigger-date").value.replaceAll("-", "") || undefined,
    strategy: document.querySelector("#trigger-strategy").value.trim() || undefined,
    online: optionalOnline(document.querySelector("#trigger-online").value),
    force: document.querySelector("#trigger-force").checked,
    ignore_gate: document.querySelector("#trigger-ignore-gate").checked,
  };
  const agentDashboardPopup = openAgentDashboard();
  try {
    const job = await request("/api/pipelines", { method: "POST", body: JSON.stringify(body), timeout: 30000 });
    handoffAgentDashboard(agentDashboardPopup, job);
    const gate = job.gate;
    const parts = [];
    parts.push(job.reused
      ? `<strong>命中已完成批次</strong>，没有重新跑：${escapeHtml(formatDate(job.trade_date))} · ${escapeHtml(job.strategy || "")} · 批次 ${escapeHtml(String(job.job_id).slice(0, 8))}`
      : `<strong>已提交</strong>：${escapeHtml(formatDate(job.trade_date))} · ${escapeHtml(job.strategy || "")} · 批次 ${escapeHtml(String(job.job_id).slice(0, 8))}`);
    if (gate) parts.push(`闸门：${gate.should_run ? "可以运行" : "暂不运行"}（${escapeHtml(GATE_LABELS[gate.reason] || gate.reason)}）${gate.detail ? ` · ${escapeHtml(gate.detail)}` : ""}`);
    note.innerHTML = parts.join("<br>");
    note.hidden = false;
    note.classList.toggle("blocked", Boolean(job.reused));
    selectedStep = null;
    if (job.reused) {
      const task = await request(`/api/pipelines/${encodeURIComponent(job.job_id)}`);
      renderPipeline(task);
      setGroupAsOf(task.result?.as_of);
      await Promise.all([loadHistory(), loadGroups()]);
      setStatus("命中已完成批次", "ready");
      return;
    }
    activeJob = { id: job.job_id, kind: job.kind };
    renderPipeline({ ...job, task_id: job.job_id, result: { steps: [] } });
    setStatus("一键流程已提交", "active");
    await loadHistory();
    schedulePoll(true);
  } catch (error) {
    button.disabled = false;
    showError(error);
    setStatus("提交被拒绝", "error");
  }
}

async function startBackfill(event) {
  event?.preventDefault();
  clearError();
  const button = document.querySelector("#backfill-run");
  const note = document.querySelector("#backfill-result");
  button.disabled = true;
  const body = {
    count: Number(document.querySelector("#backfill-count").value) || 1,
    strategy: document.querySelector("#backfill-strategy").value.trim() || undefined,
    online: optionalOnline(document.querySelector("#backfill-online").value),
    force: document.querySelector("#backfill-force").checked,
  };
  try {
    const job = await request("/api/pipelines/backfill", { method: "POST", body: JSON.stringify(body), timeout: 30000 });
    const dates = job.dates || [];
    note.innerHTML = `<strong>${job.reused ? "命中已完成的补齐批次" : "补齐已提交"}</strong>：共 ${formatNumber(job.count, 0)} 天`
      + `${dates.length ? ` · ${escapeHtml(formatDate(dates[0]))} 到 ${escapeHtml(formatDate(dates[dates.length - 1]))}` : ""}`
      + ` · 批次 ${escapeHtml(String(job.job_id).slice(0, 8))}`;
    note.hidden = false;
    note.classList.toggle("blocked", Boolean(job.reused));
    if (!job.reused) {
      activeJob = { id: job.job_id, kind: job.kind };
      setStatus("补齐任务已提交", "active");
      schedulePoll(true);
    } else {
      button.disabled = false;
      setStatus("命中已完成的补齐批次", "ready");
    }
    historyKind = job.kind;
    await loadHistory();
  } catch (error) {
    button.disabled = false;
    showError(error);
    setStatus("补齐被拒绝", "error");
  }
}

// ------------------------------------------------------------------ 轮询
async function poll() {
  if (!activeJob) return;
  try {
    const task = await request(`/api/pipelines/${encodeURIComponent(activeJob.id)}`);
    if (task.kind === "one_click_backfill") {
      const result = task.result || {};
      document.querySelector("#backfill-result").innerHTML = `<strong>补齐进行中</strong>：`
        + `${result.current_date ? `正在跑 ${escapeHtml(formatDate(result.current_date))} · ` : ""}`
        + `已完成 ${(result.completed || []).length} 天，复用 ${(result.reused || []).length} 天，共 ${(result.dates || []).length} 天`;
      document.querySelector("#backfill-result").hidden = false;
      await loadLatest();
    } else {
      renderPipeline(task);
    }
    if (["succeeded", "failed"].includes(task.status)) {
      activeJob = null;
      document.querySelector("#one-click").disabled = false;
      document.querySelector("#backfill-run").disabled = false;
      if (task.kind === "one_click_backfill") {
        const result = task.result || {};
        document.querySelector("#backfill-result").innerHTML = task.status === "succeeded"
          ? `<strong>补齐完成</strong>：新跑 ${(result.completed || []).length} 天，复用 ${(result.reused || []).length} 天`
          : `<strong>补齐失败</strong>：停在 ${escapeHtml(formatDate(task.error?.failed_date || result.failed_date))} · ${escapeHtml(task.error?.message || "无错误信息")}`;
        document.querySelector("#backfill-result").classList.toggle("blocked", task.status === "failed");
        document.querySelector("#backfill-run").disabled = false;
        await loadLatest();
      }
      setGroupAsOf(currentTask?.result?.as_of || groupAsOf);
      await Promise.all([loadHistory(), loadGroups(), loadScan()]);
      const ok = task.status === "succeeded";
      setStatus(ok ? "流程完成" : "流程失败", ok ? "ready" : "error");
      return;
    }
  } catch (error) {
    activeJob = null;
    document.querySelector("#one-click").disabled = false;
    document.querySelector("#backfill-run").disabled = false;
    showError(error);
    return;
  }
  schedulePoll(true);
}

function schedulePoll(immediate = false) {
  window.clearTimeout(pollTimer);
  if (!activeJob) return;
  pollTimer = window.setTimeout(poll, immediate ? 0 : 1200);
}

// ------------------------------------------------------------------ 表格与事件
function rowHtml(item) {
  return `<tr><td class="mono">${escapeHtml(formatNumber(item.rank, 0))}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.industry)}</td><td class="mono accent">${formatNumber(item.total, 4)}</td></tr>`;
}
function emptyRow(message, span = 4) {
  return `<tr><td colspan="${span}"><div class="empty-state">${escapeHtml(message)}</div></td></tr>`;
}

document.querySelector("#trigger-form")?.addEventListener("submit", startOneClick);
document.querySelector("#backfill-form")?.addEventListener("submit", startBackfill);
document.querySelector("#refresh")?.addEventListener("click", load);

document.querySelector("#pipeline-steps")?.addEventListener("click", (event) => {
  const node = event.target.closest("[data-step]");
  if (!node) return;
  selectedStep = selectedStep === node.dataset.step ? null : node.dataset.step;
  renderPipeline(currentTask);
});

document.querySelector("#group-tabs")?.addEventListener("click", async (event) => {
  const node = event.target.closest("[data-group]");
  if (!node) return;
  groupTab = node.dataset.group;
  groupPage = 1;
  try {
    await loadGroups();
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#group-prev")?.addEventListener("click", async () => {
  if (groupPage <= 1) return;
  groupPage -= 1;
  try {
    await loadGroups();
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#group-next")?.addEventListener("click", async () => {
  groupPage += 1;
  try {
    await loadGroups();
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#history-tabs")?.addEventListener("click", async (event) => {
  const node = event.target.closest("[data-kind]");
  if (!node) return;
  historyKind = node.dataset.kind;
  try {
    await loadHistory();
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#history-rows")?.addEventListener("click", async (event) => {
  const row = event.target.closest("[data-job]");
  if (!row) return;
  try {
    const task = await request(`/api/pipelines/${encodeURIComponent(row.dataset.job)}`);
    if (task.kind === "one_click_backfill") {
      const result = task.result || {};
      const note = document.querySelector("#backfill-result");
      note.innerHTML = `<strong>补齐批次 ${escapeHtml(String(task.job_id).slice(0, 8))}</strong>（${escapeHtml(TASK_LABELS[task.status] || task.status)}）：`
        + `共 ${(result.dates || []).length} 天 · 新跑 ${(result.completed || []).length} 天 · 复用 ${(result.reused || []).length} 天`
        + `${task.error ? `<br>停在 ${escapeHtml(formatDate(task.error.failed_date))} · ${escapeHtml(task.error.message || "")}` : ""}`;
      note.hidden = false;
      note.classList.toggle("blocked", task.status === "failed");
      setStatus("已展开补齐批次", "ready");
      return;
    }
    selectedStep = null;
    renderPipeline(task);
    setGroupAsOf(task.result?.as_of);
    await loadGroups();
    setStatus("已展开历史批次", "ready");
  } catch (error) {
    showError(error);
  }
});

renderPipeline(null);
renderGroupTabs();
load();
