// p1_desk.js · 选股台控制器
// 候选池来自 /api/stocks（真实扫描结果）；自选股来自 /api/watchlist；行业资金流来自 /api/sentiment
import { clearError, getWorkContext, initShell, setLoading, setStatus, setWorkContext, showError, workContextParams } from "/assets/js/app-shell.js";
import { query, request } from "/assets/js/api.js";
import { createTaskPanel } from "/assets/js/task-panel.js";
import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("desk");
const scanPanel = createTaskPanel(document.querySelector("#scan-task-panel"), { title: "方法论选股进度" });
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
    ...workContextParams(),
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
    const candidateCodes = (data.items || []).filter((item) => item.passed).map((item) => item.ts_code).filter(Boolean);
    // 用户在侧栏锁定了批次时，绝不能把接口返回的 run_id 写回上下文——那会把用户的选择
    // 覆盖成「这次实际查到的批次」，下拉框跳回原值，切换看着像没反应。
    // 只有未锁定（跟随最新）时才由本页回填，让侧栏显示当前实际看的是哪一批。
    const locked = Boolean(getWorkContext().run_id);
    setWorkContext({
      ...(locked ? {} : { run_id: data.run_id, as_of: data.as_of, strategy: data.strategy }),
      data_cutoff: data.data_cutoff || data.data_cutoff_at,
      availability: data.availability,
      missing_reason: data.missing_reason,
      candidate_codes: candidateCodes,
    });
    fillIndustries(data.items);
    document.querySelector("#result-count").textContent = `${data.meta.total} 只候选`;
    // 入选日期做成醒目 chip：用户反馈总览和选股台看不出「这是哪一天/哪一次的名单」。
    // `/api/stocks` 不返回批次运行时刻，所以只说日期与是否锁定，不编时间。
    const chip = document.querySelector("#desk-signal-chip");
    if (chip) {
      const lockedLabel = locked ? "已锁定所选批次" : "未锁定 · 显示最新一次";
      const batch = data.run_id ? ` · 批次 ${String(data.run_id).slice(0, 8)}` : "";
      chip.textContent = `入选日期 ${formatDate(data.as_of)} · ${lockedLabel}${batch}`;
    }
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
  scanPanel.reset();
  try {
    const job = await request("/api/scans", { method: "POST", body: JSON.stringify({ strategy: "strong_mainup", online, record: true }) });
    setStatus("扫描运行中", "active");
    const result = await pollJob(job.job_id);
    await loadStocks();
    if (result?.result?.run_id) {
      setWorkContext({ run_id: result.result.run_id, strategy: result.result.strategy, as_of: result.result.as_of, data_cutoff: result.result.data_cutoff_at, candidate_codes: result.result.candidate_codes || undefined });
    }
    setStatus("方法论选股完成", "ready");
  } catch (error) {
    showError(error);
    setStatus("选股失败", "error");
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function pollJob(jobId) {
  while (true) {
    const job = await request(`/api/scans/${jobId}`);
    scanPanel.update(job);
    document.querySelector("#scan-progress").textContent = job.status;
    if (job.status === "succeeded") return job;
    if (job.status === "failed") throw new Error(job.error?.message || "扫描失败");
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}
async function resumeScan() {
  const data = await query("/api/scans", { limit: 10 });
  const scans = data.items || [];
  const live = scans.find((item) => item.status === "queued" || item.status === "running");
  const done = scans.find((item) => item.status === "succeeded");
  if (live) {
    setStatus("扫描已恢复", "active");
    await pollJob(live.job_id || live.task_id);
    await loadStocks();
    return;
  }
  if (done) {
    scanPanel.update(done);
    document.querySelector("#scan-progress").textContent = done.status;
    setStatus("已载入最近扫描", "ready");
  }
}


document.querySelectorAll(".filters .field").forEach((field) => field.addEventListener("change", loadStocks));
document.querySelector("#search")?.addEventListener("input", () => { clearTimeout(window.searchTimer); window.searchTimer = setTimeout(loadStocks, 250); });
document.querySelectorAll("[data-scan]").forEach((button) => button.addEventListener("click", () => startScan(button.dataset.scan === "online")));
document.querySelector("#watch-add-btn")?.addEventListener("click", addWatchFromInput);
document.querySelector("#watch-add-code")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); addWatchFromInput(); }
});
// 侧栏切换入选批次后重载候选池：批次是全局上下文，页面必须跟着走，
// 否则侧栏显示批次 A 而表格还是批次 B 的名单。
window.addEventListener("hermes:batch-changed", () => { loadStocks().catch(showError); });
loadStocks().then(resumeScan).catch(showError);
loadWatchlist();
loadIndustryMoneyflow();


