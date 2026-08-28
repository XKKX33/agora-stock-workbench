import { query, request } from "/assets/js/api.js";
import { clearError, getWorkContext, initShell, setLoading, setStatus, setWorkContext, showError, workContextParams } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, statusTag } from "/assets/js/format.js";
import { aiState, newsState, reviewState } from "/assets/js/data-links.js";

initShell("overview");

const ROLE_LABELS = { methodology: "方法论", sentiment: "舆情", trend: "走势", funds: "资金", risk_chair: "风控", debate: "多空辩论" };
const AGENT_ROLES = ["methodology", "sentiment", "trend", "funds", "risk_chair", "debate"];
const TASK_STATUS = { queued: ["排队中", "pending"], running: ["运行中", "active"], succeeded: ["已完成", "good"], failed: ["失败", "bad"] };
let activePoll = null;
let agentEvents = [];

const $ = (selector) => document.querySelector(selector);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const jobId = (job) => job?.job_id || job?.task_id || job?.run_id;
const DEBATE_ROLES = ["bull", "bear", "bull_counter"];
const taskProgress = (task) => task?.result?.progress || task?.progress || {};
const taskLogs = (task) => taskProgress(task).logs || [];
const taskStatus = (status) => TASK_STATUS[status] || [status || "未知", "muted"];
// 克制俏皮的展示外号：只出现在总览页黑板，不改落库数据。
const ROLE_NICKNAMES = {
  methodology: "数浪大师", sentiment: "吃瓜记者", trend: "看盘老师傅",
  bull: "死多头", bear: "挑刺空头", bull_counter: "多头回怼", risk_chair: "风控掌柜",
};
const STAGE_TIPS = { analysis: "分析阶段", debate: "辩论阶段" };
const LOG_FLAVOR = {
  "预检配置": "清点行囊，检查装备",
  "确认交易日历": "翻日历，看今天开不开市",
  "更新市场数据": "开仓补货，把最新行情搬进库",
  "回填历史收益": "翻旧账，把历史收益补一补",
  "检查数据完整性": "给数据做个全身体检",
  "执行规则扫描": "拿起放大镜，全市场过一遍筛子",
  "采集板块舆情": "探头进热榜，看看大家在聊啥",
  "进行 Agent 研判": "智囊团就位，开始逐只会诊",
  "落库实验结果": "记账收尾，把结论落进账本",
};

// agent 进度 → 俏皮一句话（信息不丢：阶段 + 外号 + 代码 + 步数）。
function agentProgressLine(progress) {
  const role = progress.role;
  const code = progress.ts_code;
  const nick = ROLE_NICKNAMES[role] || ROLE_LABELS[role] || role;
  const stageLabel = STAGE_TIPS[progress.stage] || "";
  const prefix = code ? `【${code}】` : "";
  const step = Number(progress.step ?? 0);
  const total = Number(progress.total ?? 0);
  const count = total ? `（第 ${step}/${total} 步）` : "";
  if (!role) return `${stageLabel} 智囊团热身中${count}`;
  return `${stageLabel} ${nick}${prefix ? ` 正在盘 ${prefix}` : " 发言中"}${count}`;
}

// 九步「正在XX…」→ 俏皮；不认识的保留原文。
function flavorLogMessage(msg) {
  const m = String(msg || "");
  if (!m.startsWith("正在")) return m;
  for (const [label, flavor] of Object.entries(LOG_FLAVOR)) {
    if (m.startsWith(`正在${label}`)) return `正在${flavor}…`;
  }
  return m;
}

// 进度条上方一句话：agent 走进度翻译，九步走「正在」翻译，其余保底。
function headerMessage(task, progress) {
  if (progress?.stage === "analysis" || progress?.stage === "debate") return agentProgressLine(progress);
  if (progress?.message && String(progress.message).startsWith("正在")) return flavorLogMessage(progress.message);
  return progress?.message || task.error?.message || "等待后台状态";
}
const roleContent = (event) => {
  const value = event?.content_json ?? event?.content;
  if (value && typeof value === "object") return value.summary || value.thesis || value.verdict || value.message || JSON.stringify(value, null, 2);
  try { return JSON.parse(value || "{}").summary || JSON.parse(value || "{}").message || String(value || "暂无公开内容"); } catch { return String(value || "暂无公开内容"); }
};

function setBlackboardState(status, message = "") {
  const [label, kind] = taskStatus(status);
  const state = $("#blackboard-state");
  if (state) { state.className = `status-tag ${kind}`; state.textContent = label; }
  const overview = $("#overview-status");
  if (overview) { overview.className = `status-tag ${kind}`; overview.textContent = label; }
  if (message) $("#overview-progress-message").textContent = message;
}

function appendLocalLog(message, level = "info", detail = "") {
  const host = $("#overview-blackboard-logs");
  if (!host) return;
  const row = document.createElement("div");
  row.className = `task-log-line ${level}`;
  row.innerHTML = `<time>${new Date().toLocaleTimeString("zh-CN", { hour12: false })}</time><span><b>${escapeHtml(message)}</b>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</span>`;
  host.querySelector(".empty-state")?.remove();
  host.appendChild(row);
  host.scrollTop = host.scrollHeight;
}

function renderBlackboard(task, title = "后台任务") {
  if (!task) return;
  const progress = taskProgress(task);
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || (task.status === "succeeded" ? 100 : 0)));
  const bar = $("#overview-progress-bar");
  if (bar) bar.style.width = `${percent}%`;
  $("#overview-progress-percent").textContent = `${percent}%`;
  $("#overview-progress-message").textContent = headerMessage(task, progress);
  $("#blackboard-meta").textContent = `${title} · ${jobId(task) ? `任务 ${String(jobId(task)).slice(0, 8)}` : "未提交"} · ${task.trade_date || task.result?.as_of || "交易日待定"}`;
  setBlackboardState(task.status, headerMessage(task, progress));
  const host = $("#overview-blackboard-logs");
  const logs = taskLogs(task);
  if (logs.length) {
    host.innerHTML = logs.map((log) => `<div class="task-log-line ${escapeHtml(log.level || "info")}"><time>${escapeHtml(String(log.at || "").slice(11, 19))}</time><span><b>${escapeHtml(flavorLogMessage(log.message))}</b>${log.detail && log.detail !== log.message ? `<small>${escapeHtml(log.detail)}</small>` : ""}</span></div>`).join("");
    host.scrollTop = host.scrollHeight;
  }
  if (task.status === "failed") appendLocalLog(task.error?.message || "任务失败", "error");
}

function setButtonsDisabled(disabled) {
  document.querySelectorAll("#overview-scan,#overview-news,#overview-agents,#overview-refresh,#overview-pipeline").forEach((button) => { button.disabled = disabled; });
}

async function pollTask(path, title, { onTask, terminal = ["succeeded", "failed"], timeout = 3600000 } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const task = await request(path);
    renderBlackboard(task, title);
    await onTask?.(task);
    if (terminal.includes(task.status)) return task;
    await sleep(700);
  }
  throw new Error(`${title}等待超时，后台任务仍未给出终态`);
}

async function startScan() {
  const job = await request("/api/scans", { method: "POST", body: JSON.stringify({ strategy: "strong_mainup", online: true, record: true, force: true }) });
  const result = await pollTask(`/api/scans/${job.job_id}`, "在线选股", { onTask: (task) => { if (task.result?.run_id) setWorkContext({ run_id: task.result.run_id, as_of: task.result.as_of, strategy: task.result.strategy, candidate_codes: task.result.candidate_codes }); } });
  setWorkContext({ run_id: result.result?.run_id, as_of: result.result?.as_of, strategy: result.result?.strategy, candidate_codes: result.result?.candidate_codes });
  return result;
}

async function startNews() {
  const job = await request("/api/news/collect", { method: "POST", body: JSON.stringify({ force: true }) });
  return pollTask(`/api/news/collect/${job.job_id}`, "板块舆情采集");
}

// 总览是「最新动态」性质：不做股票选择器（那是 p13 看板的职责），但每格必须标明
// 这条发言属于哪只股票。20 只候选共用同一批角色名，不标的话六格看起来像在讲同一只，
// 实际可能来自六只不同的票——p13 那边实测就出现过这种拼接。
const eventStock = (event) => {
  const value = event?.content_json ?? event?.content;
  const payload = value && typeof value === "object" ? value : (() => { try { return JSON.parse(value || "{}"); } catch { return {}; } })();
  return String(payload.ts_code ?? event?.ts_code ?? "");
};
const stockPrefix = (event) => { const code = eventStock(event); return code ? `【${code}】` : ""; };

function updateAgentPanels(events) {
  const latest = new Map();
  events.forEach((event) => { if (event?.role) latest.set(event.role, event); });
  AGENT_ROLES.forEach((role) => {
    const panel = $(`.agent-mini[data-role="${role}"]`);
    const content = panel?.querySelector("[data-role-content]");
    if (!content) return;
    if (role === "debate") {
      const debateEvents = DEBATE_ROLES.map((name) => latest.get(name)).filter(Boolean);
      content.textContent = debateEvents.length ? debateEvents.map((event) => `${stockPrefix(event)}${ROLE_LABELS[event.role] || event.role}：${roleContent(event)}`).join("\n\n") : "尚未收到多空发言";
      panel.classList.toggle("has-content", debateEvents.length > 0);
      panel.open = debateEvents.some((event) => event.event_type === "message.delta");
      return;
    }
    const sourceRole = role === "funds" ? "trend" : role;
    const event = latest.get(sourceRole);
    content.textContent = event ? (role === "funds" ? `${stockPrefix(event)}复用走势角色的资金证据：\n${roleContent(event)}` : `${stockPrefix(event)}${roleContent(event)}`) : (role === "funds" ? "资金证据将随走势分析一并到达" : "尚未收到发言");
    panel.classList.toggle("has-content", Boolean(event));
    if (event?.event_type === "message.delta") panel.open = true;
  });
  const active = [...latest.values()].find((event) => event?.event_type === "message.delta") || [...latest.values()].at(-1);
  if (active) {
    const code = eventStock(active);
    $("#overview-agent-meta").textContent = `${ROLE_NICKNAMES[active.role] || ROLE_LABELS[active.role] || "公开消息"} 正在发言${code ? ` · ${code}` : ""}`;
    appendLocalLog(`${ROLE_NICKNAMES[active.role] || ROLE_LABELS[active.role] || "公开消息"}${code ? ` 盯上了 ${code}` : " 发言中"}`, "info", roleContent(active).slice(0, 120));
  }
}

async function pollAgent(job) {
  const id = jobId(job);
  let afterSeq = 0;
  while (true) {
    const [task, events] = await Promise.all([
      request(`/api/agents/jobs/${id}`),
      query(`/api/agents/jobs/${id}/events`, { after_seq: afterSeq, limit: 500 }).catch(() => ({ items: [], next_seq: afterSeq })),
    ]);
    const fresh = (events.items || []).filter((event) => Number(event.seq || 0) > afterSeq);
    if (fresh.length) { agentEvents = [...agentEvents, ...fresh]; afterSeq = Number(events.next_seq || fresh.at(-1).seq || afterSeq); updateAgentPanels(agentEvents); }
    renderBlackboard({ ...task, progress: task.progress }, "Agent 辩论");
    const prog = task.progress;
    $("#overview-agent-meta").textContent = (prog?.stage === "analysis" || prog?.stage === "debate") ? agentProgressLine(prog) : (prog?.message || `已收到 ${agentEvents.length} 条公开消息`);
    if (["succeeded", "failed"].includes(task.status)) return task;
    await sleep(700);
  }
}

async function startAgents() {
  const context = getWorkContext();
  if (!context.run_id || !context.candidate_codes?.length) throw new Error("还没有成功的选股候选池，请先运行在线选股");
  agentEvents = [];
  document.querySelectorAll(".agent-mini").forEach((panel) => { panel.open = false; });
  const job = await request("/api/agents/judge", { method: "POST", body: JSON.stringify({ run_id: context.run_id, as_of: context.as_of, ts_codes: context.candidate_codes, force: true }) });
  return pollAgent(job);
}

async function startPipeline() {
  const job = await request("/api/pipelines", { method: "POST", body: JSON.stringify({ online: true, force: true }) });
  const id = jobId(job);
  let afterSeq = 0;
  // agents 阶段把 Agent 公开发言实时追加进黑板；其余阶段只渲染步骤日志。
  const onTask = async (task) => {
    const stepName = task?.result?.progress?.stage || task?.result?.current_step;
    if (stepName !== "agents" || !id) return;
    try {
      const agentTask = await request(`/api/agents/jobs/${id}`);
      const prog = agentTask?.progress;
      if (prog && (prog.stage === "analysis" || prog.stage === "debate")) {
        $("#overview-progress-message").textContent = agentProgressLine(prog);
      }
      const events = await query(`/api/agents/jobs/${id}/events`, { after_seq: afterSeq, limit: 500 });
      const fresh = (events.items || []).filter((event) => Number(event.seq || 0) > afterSeq);
      if (!fresh.length) return;
      afterSeq = Number(events.next_seq || fresh.at(-1).seq || afterSeq);
      agentEvents = [...agentEvents, ...fresh];
      updateAgentPanels(agentEvents);
    } catch { /* 事件流暂时不可用不阻塞主进度轮询 */ }
  };
  return pollTask(`/api/pipelines/${id}`, "一键在线全流程", { onTask, timeout: 7200000 });
}

async function run(action, title) {
  if (activePoll) return;
  activePoll = action;
  clearError();
  setButtonsDisabled(true);
  setBlackboardState("running", `${title}启动中`);
  appendLocalLog(`黑板就绪，${title}开工`, "info");
  try {
    await action();
    setStatus(`${title}完成`, "ready");
    appendLocalLog(`${title}收工，结论已落库`, "info");
    await load();
  } catch (error) {
    showError(error);
    setStatus(`${title}失败`, "error");
    setBlackboardState("failed", error.message);
    appendLocalLog(`${title}翻车了，原因见下`, "error", error.message);
  } finally {
    activePoll = null;
    setButtonsDisabled(false);
  }
}

function renderMetrics(data) {
  const scan = data.latest_scan;
  $("#metric-candidates").textContent = scan ? formatNumber(scan.candidate_count, 0) : "—";
  $("#metric-scored").textContent = scan ? formatNumber(scan.scored_count, 0) : "—";
  $("#metric-passed").textContent = scan ? formatNumber(scan.passed_count, 0) : "—";
  $("#metric-final").textContent = scan ? formatNumber(scan.final_count, 0) : "—";
  $("#scan-state").innerHTML = data.scan_job ? statusTag(data.scan_job.status, "active") : statusTag("空闲", "good");
}

function renderPicks(picks) {
  const body = $("#pick-rows");
  if (!picks.length) { body.innerHTML = `<tr><td colspan="6"><div class="empty-state">尚无入选股票<br>先运行在线选股</div></td></tr>`; return; }
  body.innerHTML = picks.map((item) => `<tr data-code="${escapeHtml(item.ts_code)}"><td class="mono">${item.rank}</td><td><strong>${escapeHtml(item.name)}</strong><br><span class="muted mono">${escapeHtml(item.ts_code)}</span></td><td>${escapeHtml(item.industry)}</td><td class="mono accent">${formatNumber(item.total, 4)}</td><td>${statusTag(item.money_class || "未确认")}</td><td class="muted">${escapeHtml(item.one_line)}</td></tr>`).join("");
  body.querySelectorAll("tr[data-code]").forEach((row) => row.addEventListener("click", () => { location.href = `p1_desk.html?code=${encodeURIComponent(row.dataset.code)}`; }));
}

function renderTables(tables) {
  const body = $("#table-status");
  body.innerHTML = Object.entries(tables).map(([name, item]) => `<div class="kv"><span>${escapeHtml(name)}</span><span>${formatNumber(item.row_count, 0)} · ${formatDate(item.latest_date)}</span></div>`).join("");
}

async function renderLinkStatus() {
  const container = $("#table-status");
  if (!container) return;
  container.querySelectorAll("[data-link-row]").forEach((el) => el.remove());
  const states = await Promise.all([newsState(), reviewState(), aiState()]);
  container.insertAdjacentHTML("beforeend", states.map((state, index) => `<div class="kv" data-link-row title="${escapeHtml(state.detail)}"><span>${["舆情", "复盘", "AI 复盘"][index]}</span><span class="link-state ${state.kind}">${escapeHtml(state.label)}</span></div>`).join(""));
}

async function load() {
  clearError();
  try {
    const data = await request("/api/overview");
    $("#trade-date").textContent = formatDate(data.latest_trade_date);
    // 「最新入选」标明是哪一天哪一批：总览固定显示最新一次（要切历史批次去台账），
    // 但至少让用户知道眼前这 6 只属于哪次运行。
    const chip = $("#picks-signal");
    const scan = data.latest_scan;
    if (chip) {
      if (scan?.as_of) {
        const batch = scan.run_id ? ` · 批次 ${String(scan.run_id).slice(0, 8)}` : "";
        chip.textContent = `入选日期 ${formatDate(scan.as_of)}${batch}`;
        chip.hidden = false;
      } else {
        chip.hidden = true;
      }
    }
    renderMetrics(data); renderPicks(data.latest_scan?.picks || []); renderTables(data.tables || {}); renderLinkStatus();
    $("#updated-at").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
    if (!activePoll) setStatus("DuckDB 已连接", "ready");
  } catch (error) { showError(error); }
}

$("#overview-scan")?.addEventListener("click", () => run(startScan, "在线选股"));
$("#overview-news")?.addEventListener("click", () => run(startNews, "板块舆情采集"));
$("#overview-agents")?.addEventListener("click", () => run(startAgents, "Agent 辩论"));
$("#overview-pipeline")?.addEventListener("click", () => run(startPipeline, "一键在线全流程"));
$("#overview-refresh")?.addEventListener("click", load);
load();