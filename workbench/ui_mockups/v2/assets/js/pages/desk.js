// p1_desk.js · 选股台控制器
// 候选池来自 /api/stocks（真实扫描结果）；自选股来自 /api/watchlist；行业资金流来自 /api/sentiment
import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, statusTag } from "/assets/js/format.js";

initShell("desk");
let currentCode = new URLSearchParams(location.search).get("code");
let watchItems = [];
let watchCodes = new Set();

// 涨红跌绿：正数红、负数绿、0 或空无色
function signClass(value) {
  if (value == null || Number.isNaN(Number(value)) || Number(value) === 0) return "";
  return Number(value) > 0 ? "up" : "down";
}

function pctText(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  return `${num > 0 ? "+" : ""}${formatNumber(num)}%`;
}

/* ---------- 候选池（选股工作流主体） ---------- */

async function loadStocks() {
  clearError();
  setLoading(true);
  const params = {
    search: document.querySelector("#search").value,
    industry: document.querySelector("#industry").value,
    passed: document.querySelector("#passed").value,
    sort: document.querySelector("#sort").value,
    order: document.querySelector("#order").value,
    per_page: 200,
  };
  try {
    const data = await query("/api/stocks", params);
    renderRows(data.items);
    fillIndustries(data.items);
    document.querySelector("#result-count").textContent = `${data.meta.total} 只候选`;
    setStatus(`扫描截面 ${data.as_of}`, "ready");
    if (currentCode) await loadDetail(currentCode);
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

function renderRows(items) {
  const body = document.querySelector("#stock-rows");
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="8"><div class="empty-state">没有符合当前条件的股票</div></td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr data-code="${escapeHtml(item.ts_code)}">
      <td><button type="button" class="star-btn ${watchCodes.has(item.ts_code) ? "in-list" : ""}" data-act="watch" aria-label="加入或移除自选">${watchCodes.has(item.ts_code) ? "★" : "☆"}</button></td>
      <td class="mono">${item.rank}</td><td><strong>${escapeHtml(item.name)}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td>
      <td>${escapeHtml(item.industry)}</td><td class="mono accent">${formatNumber(item.total, 4)}</td>
      <td>${item.passed ? statusTag("通过", "good") : statusTag("淘汰", "bad")}</td>
      <td>${item.selected ? statusTag("入选", "active") : "—"}</td><td>${statusTag(item.money_class || "未确认")}</td>
    </tr>`).join("");
  body.querySelectorAll("tr[data-code]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("[data-act='watch']")) return;
      loadDetail(row.dataset.code);
    });
  });
  body.querySelectorAll("[data-act='watch']").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleWatch(btn.closest("tr").dataset.code);
    });
  });
}

function fillIndustries(items) {
  const select = document.querySelector("#industry");
  if (select.dataset.ready) return;
  [...new Set(items.map((item) => item.industry).filter(Boolean))].sort().forEach((industry) => {
    select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(industry)}">${escapeHtml(industry)}</option>`);
  });
  select.dataset.ready = "1";
}

/* ---------- 自选股 ---------- */

async function loadWatchlist() {
  clearError();
  try {
    const data = await query("/api/watchlist", { per_page: 200, sort: "sort_order", order: "asc" });
    watchItems = data.items || [];
    watchCodes = new Set(watchItems.map((item) => item.ts_code));
    renderWatchRows();
    renderWatchStars();
  } catch (error) {
    showError(error);
  }
}

function renderWatchRows() {
  const body = document.querySelector("#watch-rows");
  if (!body) return;
  if (!watchItems.length) {
    body.innerHTML = `<tr class="watch-empty"><td colspan="5"><div class="empty-state">还没有自选股：在候选池点星标，或在上方输入代码添加</div></td></tr>`;
    return;
  }
  body.innerHTML = watchItems.map((row) => `
    <tr class="watch-row" data-code="${escapeHtml(row.ts_code)}">
      <td><strong>${escapeHtml(row.name || "—")}</strong><br><span class="mono muted">${escapeHtml(row.ts_code)}</span></td>
      <td class="mono ${signClass(row.pct_chg)}">${formatNumber(row.close)}</td>
      <td class="mono ${signClass(row.pct_chg)}">${pctText(row.pct_chg)}</td>
      <td class="mono muted">${row.last_date ? escapeHtml(formatDate(row.last_date)) : "—"}</td>
      <td><button type="button" class="button watch-remove" data-act="remove" aria-label="移除自选">移除</button></td>
    </tr>`).join("");
  body.querySelectorAll(".watch-row").forEach((tr) => {
    tr.addEventListener("click", (event) => {
      if (event.target.closest("[data-act='remove']")) return;
      location.href = `p6_chart.html?code=${encodeURIComponent(tr.dataset.code)}`;
    });
  });
  body.querySelectorAll("[data-act='remove']").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleWatch(btn.closest("tr").dataset.code);
    });
  });
}

function renderWatchStars() {
  document.querySelectorAll("#stock-rows tr[data-code]").forEach((row) => {
    const btn = row.querySelector("[data-act='watch']");
    if (!btn) return;
    const inList = watchCodes.has(row.dataset.code);
    btn.classList.toggle("in-list", inList);
    btn.textContent = inList ? "★" : "☆";
  });
}

async function toggleWatch(code) {
  clearError();
  try {
    if (watchCodes.has(code)) {
      await request(`/api/watchlist/${encodeURIComponent(code)}`, { method: "DELETE" });
    } else {
      await request("/api/watchlist", { method: "POST", body: JSON.stringify({ ts_code: code }) });
    }
    await loadWatchlist();
  } catch (error) {
    showError(error);
  }
}

async function addWatchFromInput() {
  const raw = document.querySelector("#watch-add-code").value.trim();
  if (!raw) return;
  clearError();
  try {
    const items = (await query("/api/kline/search", { q: raw, limit: 5 })).items || [];
    const upper = raw.toUpperCase();
    const exact = items.find((item) => item.ts_code === upper || item.symbol === raw);
    const target = exact || items[0];
    if (!target) throw new Error(`没有找到 "${raw}" 对应的股票`);
    await request("/api/watchlist", { method: "POST", body: JSON.stringify({ ts_code: target.ts_code }) });
    document.querySelector("#watch-add-code").value = "";
    await loadWatchlist();
  } catch (error) {
    showError(error);
  }
}

/* ---------- 行业资金流向 ---------- */

async function loadIndustryMoneyflow() {
  clearError();
  try {
    const data = await request("/api/sentiment");
    renderIndustryMoneyflow(data.industry_moneyflow || {});
  } catch (error) {
    showError(error);
  }
}

function renderIndustryMoneyflow(mf) {
  const body = document.querySelector("#industry-mf-rows");
  const note = document.querySelector("#mf-industry-note");
  if (!body) return;
  const items = mf.items || [];
  if (mf.availability !== "available" || !items.length) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty-state">暂无行业资金流数据${mf.reason ? `（${escapeHtml(mf.reason)}）` : ""}</div></td></tr>`;
    if (note) note.textContent = "数据尚未采集或最新交易日无记录";
    return;
  }
  const range = mf.date_range || [];
  const rangeText = range.length === 2 ? `${formatDate(range[0])} ~ ${formatDate(range[1])}` : "—";
  if (note) note.textContent = `截至 ${formatDate(mf.as_of)} · 覆盖 ${formatNumber(mf.stock_count, 0)} 只股票 · 数据区间 ${rangeText} · 净流入红涨绿跌`;
  // 右侧栏空间有限，只展示净流入居前的 12 个行业
  const top = [...items].sort((a, b) => (b.net_mf_amount ?? 0) - (a.net_mf_amount ?? 0)).slice(0, 12);
  body.innerHTML = top.map((item) => {
    const net = item.net_mf_amount;
    const lg = (item.buy_lg_amount ?? 0) - (item.sell_lg_amount ?? 0);
    const elg = (item.buy_elg_amount ?? 0) - (item.sell_elg_amount ?? 0);
    return `<tr>
      <td>${escapeHtml(item.industry || "未知")}</td>
      <td class="mono ${signClass(net)}">${formatNumber(net, 0)}</td>
      <td class="mono ${signClass(lg)}">${formatNumber(lg, 0)}</td>
      <td class="mono ${signClass(elg)}">${formatNumber(elg, 0)}</td>
      <td class="mono">${formatNumber(item.stock_count, 0)}</td>
    </tr>`;
  }).join("");
}

/* ---------- 个股详情 ---------- */

async function loadDetail(code) {
  currentCode = code;
  history.replaceState(null, "", `?code=${encodeURIComponent(code)}`);
  try {
    const item = await request(`/api/stocks/${encodeURIComponent(code)}`);
    document.querySelector("#detail-title").textContent = `${item.name} · ${item.ts_code}`;
    document.querySelector("#detail-summary").textContent = item.one_line || "暂无归因";
    document.querySelector("#detail-meta").innerHTML = `
      <div class="kv"><span>综合分</span><span>${formatNumber(item.total, 4)}</span></div>
      <div class="kv"><span>行业</span><span>${escapeHtml(item.industry)}</span></div>
      <div class="kv"><span>门槛</span><span>${item.passed ? "通过" : escapeHtml((item.gate_reasons || []).join("；") || "未通过")}</span></div>
      <div class="kv"><span>资金确认</span><span>${escapeHtml(item.money_class || "未确认")}</span></div>`;
    renderFactors(item.factors || {});
    renderHistory(item.history || []);
  } catch (error) {
    showError(error);
  }
}

function renderFactors(factors) {
  const rows = Object.entries(factors).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 10);
  const max = Math.max(...rows.map(([, value]) => Math.abs(value)), 0.001);
  document.querySelector("#factor-bars").innerHTML = rows.map(([name, value]) => `
    <div class="bar-row"><span>${escapeHtml(name)}</span><span class="bar-track"><span class="bar-fill" style="width:${Math.abs(value) / max * 100}%"></span></span><span class="mono">${formatNumber(value, 4)}</span></div>`).join("") || `<div class="empty-state">暂无因子数据</div>`;
}

function renderHistory(history) {
  const recent = (history || []).slice(-8).reverse();
  document.querySelector("#history-rows").innerHTML = recent.map((row) => {
    const pct = row.pct_chg == null ? "—" : `${formatNumber(row.pct_chg)}%`;
    const cls = row.pct_chg == null ? "muted" : signClass(row.pct_chg);
    return `<tr><td class="mono">${escapeHtml(row.trade_date)}</td><td class="mono">${formatNumber(row.close)}</td><td class="mono ${cls}">${pct}</td><td class="mono">${formatNumber(row.amount, 0)}</td></tr>`;
  }).join("") || `<tr><td colspan="4"><div class="empty-state">暂无行情数据</div></td></tr>`;
}

/* ---------- 扫描 ---------- */

async function startScan(online) {
  const buttons = document.querySelectorAll("[data-scan]");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const job = await request("/api/scans", { method: "POST", body: JSON.stringify({ strategy: "strong_mainup", online, record: true }) });
    setStatus("扫描运行中", "");
    await pollJob(job.job_id);
    await loadStocks();
  } catch (error) {
    showError(error);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function pollJob(jobId) {
  while (true) {
    const job = await request(`/api/scans/${jobId}`);
    document.querySelector("#scan-progress").textContent = job.status;
    if (job.status === "succeeded") return job;
    if (job.status === "failed") throw new Error(job.error?.message || "扫描失败");
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

document.querySelectorAll(".filters .field").forEach((field) => field.addEventListener("change", loadStocks));
document.querySelector("#search")?.addEventListener("input", () => { clearTimeout(window.searchTimer); window.searchTimer = setTimeout(loadStocks, 250); });
document.querySelectorAll("[data-scan]").forEach((button) => button.addEventListener("click", () => startScan(button.dataset.scan === "online")));
document.querySelector("#watch-add-btn")?.addEventListener("click", addWatchFromInput);
document.querySelector("#watch-add-code")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); addWatchFromInput(); }
});
loadStocks();
loadWatchlist();
loadIndustryMoneyflow();


/* ---------- AI 研判 ---------- */

const AGENT_LS = "hermes.agent.params";
const agentStatusHost = document.querySelector("#agent-status");
const agentPoolNote = document.querySelector("#agent-pool-note");
const agentProgress = document.querySelector("#agent-progress");
const agentStage = document.querySelector("#agent-stage");
const agentStep = document.querySelector("#agent-step");
const agentBar = document.querySelector("#agent-bar");
const agentMessage = document.querySelector("#agent-message");
const agentResults = document.querySelector("#agent-results");
const agentRecent = document.querySelector("#agent-recent");
const agentRunBtn = document.querySelector("#agent-run");
const candidatesInput = document.querySelector("#agent-candidates");
const depthInput = document.querySelector("#agent-depth");
const finalInput = document.querySelector("#agent-final");
const forceCheck = document.querySelector("#agent-force");

let agentDefaults = { candidates: 200, depth: 8, final: 3 };
let agentLimits = { max_candidates: 200, max_depth: 30, max_final: 10 };
let agentJobId = null;
let agentPollTimer = null;
let agentBusy = false;

const STAGE_LABEL = {
  queued: "排队中",
  coarse: "粗筛",
  deep: "深度学习",
  debate: "辩论",
  done: "完成",
};

function readAgentParams() {
  try {
    const saved = JSON.parse(localStorage.getItem(AGENT_LS) || "{}");
    candidatesInput.value = saved.candidates ?? agentDefaults.candidates;
    depthInput.value = saved.depth ?? agentDefaults.depth;
    finalInput.value = saved.final ?? agentDefaults.final;
  } catch {
    candidatesInput.value = agentDefaults.candidates;
    depthInput.value = agentDefaults.depth;
    finalInput.value = agentDefaults.final;
  }
}

function saveAgentParams() {
  localStorage.setItem(AGENT_LS, JSON.stringify({
    candidates: Number(candidatesInput.value) || agentDefaults.candidates,
    depth: Number(depthInput.value) || agentDefaults.depth,
    final: Number(finalInput.value) || agentDefaults.final,
  }));
}

function clampAgentParams() {
  candidatesInput.value = Math.max(1, Math.min(Number(candidatesInput.value) || 1, agentLimits.max_candidates));
  depthInput.value = Math.max(1, Math.min(Number(depthInput.value) || 1, agentLimits.max_depth, Number(candidatesInput.value) || 1));
  finalInput.value = Math.max(1, Math.min(Number(finalInput.value) || 1, agentLimits.max_final, Number(depthInput.value) || 1));
  saveAgentParams();
}

function renderAgentStatus(info) {
  if (!agentStatusHost) return;
  const availability = info.availability;
  if (availability === "available") {
    agentStatusHost.innerHTML = `<span class="tag good">已配置</span> ${escapeHtml(info.provider || "openai_compatible")} · ${escapeHtml(info.model || "")}`;
    return;
  }
  const label = availability === "disabled" ? "未启用" : "未配置";
  const reason = info.reason || "AI 配置不完整";
  agentStatusHost.innerHTML = `<span class="tag">${label}</span> ${escapeHtml(reason)}`;
  agentRunBtn.disabled = availability !== "available";
}

async function loadAgentStatus() {
  clearError();
  try {
    const info = await request("/api/agents/status");
    agentDefaults = info.defaults || agentDefaults;
    agentLimits = info.limits || agentLimits;
    candidatesInput.max = agentLimits.max_candidates;
    depthInput.max = agentLimits.max_depth;
    finalInput.max = agentLimits.max_final;
    readAgentParams();
    renderAgentStatus(info);
  } catch (error) {
    showError(error);
  }
}

async function loadAgentPoolNote() {
  if (!agentPoolNote) return;
  clearError();
  try {
    const data = await query("/api/agents/candidates", { limit: 200 });
    agentPoolNote.textContent = `当前候选池 ${data.items.length} 只 · 截面 ${data.as_of || "—"}`;
  } catch (error) {
    agentPoolNote.textContent = "候选池读取失败";
  }
}

async function loadAgentRecent() {
  if (!agentRecent) return;
  clearError();
  try {
    const data = await request("/api/agents/jobs?limit=6");
    const jobs = data.items || [];
    if (!jobs.length) { agentRecent.innerHTML = ""; return; }
    agentRecent.innerHTML = `<div class="agent-empty" style="margin-bottom:4px">最近研判</div>` + jobs.map((job) => {
      const st = job.status || "unknown";
      return `<button type="button" class="agent-recent-row" data-job="${escapeHtml(job.job_id)}" data-status="${escapeHtml(st)}">
        ${job.status === "succeeded" ? "✓" : ""}${job.status === "failed" ? "✗" : ""}
        <span class="mono">${formatDate(job.trade_date || "")}</span>
        ${job.result?.final?.length ?? "?"} 只 · ${escapeHtml(job.status)}
      </button>`;
    }).join("");
    agentRecent.querySelectorAll("[data-job]").forEach((btn) => {
      btn.addEventListener("click", () => openAgentJob(btn.dataset.job, btn.dataset.status));
    });
  } catch (error) {
    agentRecent.innerHTML = "";
  }
}

async function openAgentJob(jobId, status) {
  if (!jobId) return;
  agentJobId = jobId;
  stopAgentPoll();
  clearError();
  agentProgress.hidden = false;
  agentBar.style.width = "10%";
  agentStage.textContent = "读取任务…";
  agentStep.textContent = "";
  agentMessage.textContent = "";
  if (status === "succeeded" || status === "failed") {
    await refreshAgentJob(jobId);
    return;
  }
  startAgentPoll(jobId);
}

async function startJudge() {
  if (agentBusy) return;
  clearError();
  clampAgentParams();
  const body = {
    candidates: Number(candidatesInput.value),
    depth: Number(depthInput.value),
    final: Number(finalInput.value),
    force: forceCheck.checked,
  };
  agentRunBtn.disabled = true;
  agentBusy = true;
  try {
    const job = await request("/api/agents/judge", { method: "POST", body: JSON.stringify(body) });
    agentJobId = job.job_id;
    agentProgress.hidden = false;
    agentStage.textContent = "排队中";
    agentStep.textContent = "";
    agentMessage.textContent = "任务已提交，等待开始";
    agentBar.style.width = "4%";
    startAgentPoll(job.job_id);
    loadAgentRecent();
  } catch (error) {
    showError(error);
    agentProgress.hidden = true;
  } finally {
    agentBusy = false;
    agentRunBtn.disabled = false;
    const st = await request("/api/agents/status").catch(() => null);
    if (st) renderAgentStatus(st);
  }
}

function startAgentPoll(jobId) {
  stopAgentPoll();
  agentPollTimer = setInterval(() => refreshAgentJob(jobId), 1200);
  refreshAgentJob(jobId);
}

function stopAgentPoll() {
  if (agentPollTimer) { clearInterval(agentPollTimer); agentPollTimer = null; }
}

function renderAgentProgress(progress) {
  if (!progress) return;
  const stage = progress.stage || "queued";
  agentStage.textContent = STAGE_LABEL[stage] || stage;
  const total = Number(progress.total) || 0;
  const step = Number(progress.step) || 0;
  agentStep.textContent = total ? `${step} / ${total}` : "";
  agentMessage.textContent = progress.message || "";
  const pct = total ? Math.max(4, Math.round(step / total * 100)) : 4;
  agentBar.style.width = pct + "%";
}

async function refreshAgentJob(jobId) {
  try {
    const job = await request(`/api/agents/jobs/${encodeURIComponent(jobId)}`);
    if (job.progress) renderAgentProgress(job.progress);
    if (job.status === "succeeded") {
      stopAgentPoll();
      renderJudgeResults(job);
      agentStage.textContent = "完成";
      agentBar.style.width = "100%";
      agentMessage.textContent = `研判完成 · 截面 ${job.result?.as_of || ""} · 最终 ${job.judgments?.length || 0} 只`;
      loadAgentRecent();
    } else if (job.status === "failed") {
      stopAgentPoll();
      agentStage.textContent = "失败";
      agentBar.style.width = "100%";
      agentMessage.textContent = job.error?.message || "研判失败";
      agentResults.innerHTML = "";
    }
  } catch (error) {
    showError(error);
  }
}

function stanceText(stance) {
  return { bullish: "看多", neutral: "中性", bearish: "看空" }[stance] || stance || "—";
}

function verdictClass(verdict) {
  if (verdict === "看多") return "verdict-bull";
  if (verdict === "看空") return "verdict-bear";
  return "verdict-flat";
}

function renderAnalyst(name, analyst) {
  if (!analyst) return "";
  return `<div class="agent-analyst"><b>${escapeHtml(name)}</b> <span class="mono">${formatNumber(analyst.score, 0)} · ${stanceText(analyst.stance)}</span>
    <div style="margin-top:4px">${(analyst.points || []).map((pt) => "· " + escapeHtml(pt)).join("<br>")}</div>
    ${(analyst.risks || []).length ? `<div style="color:#c49a4a;margin-top:3px">${analyst.risks.map((r) => "! " + escapeHtml(r)).join("<br>")}</div>` : ""}
  </div>`;
}

function renderJudgeResults(job) {
  if (!agentResults) return;
  const items = job.judgments || [];
  if (!items.length) { agentResults.innerHTML = `<div class="agent-empty">本次研判没有输出结论</div>`; return; }
  agentResults.innerHTML = items.map((item) => {
    const stage = item.stage || {};
    const deep = stage.deep || {};
    const debate = stage.debate || {};
    const analysts = deep.analysts || {};
    const inList = watchCodes.has(item.ts_code);
    return `
      <article class="agent-card" data-code="${escapeHtml(item.ts_code)}">
        <div class="agent-card-head">
          <strong>${escapeHtml(item.name || "—")}</strong>
          <span class="mono">${escapeHtml(item.ts_code)}</span>
          <span class="mono muted">排名 ${escapeHtml(Number(item.rank) || 0)}</span>
          <span class="spacer"></span>
          <span class="tag ${verdictClass(stage.verdict)}">${escapeHtml(stage.verdict || "未定")}</span>
        </div>
        <div class="agent-score-row"><span class="mono muted">综合 ${formatNumber(item.score, 0)}</span>
          <span class="agent-score-track"><span class="agent-score-fill" style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></span></span>
          <span class="mono muted">${stanceText(item.stance)}</span>
        </div>
        <div class="agent-thesis">${escapeHtml(item.thesis || "—")}</div>
        <div class="agent-action">操作建议：${escapeHtml(stage.action || "—")}</div>
        ${(item.risks || []).length ? `<ul class="agent-risks">${item.risks.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
        <div class="agent-source" style="font-size:11px;color:var(--text-muted);margin:2px 0 8px">数据来源：选股台扫描 / 日线·周线指标 / TrendRadar 舆情 / 资金流</div>
        <div class="agent-card-foot">
          <button type="button" class="button primary" data-act="agent-watch" style="min-height:30px;padding:0 12px;font-size:12px">${inList ? "★ 已自选" : "☆ 加入自选"}</button>
          <a class="button" href="p6_chart.html?code=${encodeURIComponent(item.ts_code)}" style="min-height:30px;padding:6px 12px;font-size:12px">看K线</a>
        </div>
        <details class="agent-detail">
          <summary>分析师详情与多空辩论</summary>
          ${renderAnalyst("方法论", analysts.methodology)}
          ${renderAnalyst("舆情", analysts.sentiment)}
          ${renderAnalyst("走势", analysts.trend)}
          ${debate.bull || debate.bear ? `<div class="agent-debate"><div><b>多方：</b>${escapeHtml(debate.bull || "—")}</div><div class="bear"><b>空方：</b>${escapeHtml(debate.bear || "—")}</div></div>` : ""}
        </details>
      </article>`;
  }).join("");
  agentResults.querySelectorAll("[data-act='agent-watch']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".agent-card");
      if (!card) return;
      const code = card.dataset.code;
      const existing = watchCodes.has(code);
      const promise = existing
        ? request(`/api/watchlist/${encodeURIComponent(code)}`, { method: "DELETE" })
        : request("/api/watchlist", { method: "POST", body: JSON.stringify({ ts_code: code }) });
      promise.then(() => loadWatchlist()).catch(showError);
    });
  });
}

[candidatesInput, depthInput, finalInput].forEach((input) => input.addEventListener("change", clampAgentParams));
agentRunBtn?.addEventListener("click", startJudge);

loadAgentStatus();
loadAgentPoolNote();
loadAgentRecent();
