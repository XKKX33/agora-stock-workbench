// p10_watchlist.js · 自选股页控制器
// 数据来自 /api/watchlist（真实自选池 + 行情），添加走 /api/kline/search 找到股票后写入自选
import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber } from "/assets/js/format.js";

initShell("watchlist");

let watchItems = [];

const searchInput = document.querySelector("#watch-search");
const industrySelect = document.querySelector("#watch-industry");
const addInput = document.querySelector("#watch-add-code");
const addBtn = document.querySelector("#watch-add-btn");
const refreshBtn = document.querySelector("#watch-refresh");
const rowsBody = document.querySelector("#watch-rows");
const statsHost = document.querySelector("#watch-stats");

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

/* ---------- 自选股 ---------- */

async function loadWatchlist() {
  clearError();
  try {
    const data = await query("/api/watchlist", { per_page: 200, sort: "sort_order", order: "asc" });
    watchItems = data.items || [];
    renderFilters();
    renderStats();
    renderRows();
    setStatus(`自选股 ${watchItems.length} 只 · 行情 ${data.as_of || "最新"}`, "ready");
  } catch (error) {
    showError(error);
  }
}

function renderFilters() {
  const current = industrySelect.value;
  const industries = [...new Set(watchItems.map((item) => item.industry).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
  industrySelect.innerHTML = `<option value="">全部行业</option>` + industries
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  industrySelect.value = industries.includes(current) ? current : "";
}

function filteredRows() {
  const keyword = (searchInput.value || "").trim().toLowerCase();
  const industry = industrySelect.value;
  return watchItems.filter((item) => {
    const hitKeyword = !keyword
      || `${item.ts_code} ${item.symbol || ""} ${item.name || ""}`.toLowerCase().includes(keyword);
    const hitIndustry = !industry || item.industry === industry;
    return hitKeyword && hitIndustry;
  });
}

function renderStats() {
  if (!statsHost) return;
  const up = watchItems.filter((item) => Number(item.pct_chg) > 0).length;
  const down = watchItems.filter((item) => Number(item.pct_chg) < 0).length;
  const flat = watchItems.length - up - down;
  statsHost.innerHTML = `
    <span class="stat-chip">自选总数 <b>${watchItems.length}</b></span>
    <span class="stat-chip">上涨 <b class="up">${up}</b></span>
    <span class="stat-chip">下跌 <b class="down">${down}</b></span>
    <span class="stat-chip">平盘/无数据 <b>${flat}</b></span>`;
}

function renderRows() {
  const rows = filteredRows();
  if (!watchItems.length) {
    rowsBody.innerHTML = `
      <tr class="watch-empty"><td colspan="7">
        <div class="guide">
          <div class="guide-icon">★</div>
          <div class="guide-title">还没有自选股</div>
          <div class="guide-tip">在「选股台」候选池点星标，或在「行情K线」页搜索后点击加入自选，<br>也可以直接在上方输入框输入代码或名称添加。</div>
          <div class="guide-actions">
            <a class="button" href="p1_desk.html">去选股台</a>
            <a class="button primary" href="p6_chart.html">去行情搜索</a>
          </div>
        </div>
      </td></tr>`;
    return;
  }
  if (!rows.length) {
    rowsBody.innerHTML = `<tr class="watch-empty"><td colspan="7"><div class="empty-state">暂无匹配的自选股，调整筛选条件看看</div></td></tr>`;
    return;
  }
  rowsBody.innerHTML = rows.map((row) => `
    <tr class="watch-row" data-code="${escapeHtml(row.ts_code)}">
      <td class="mono">${escapeHtml(row.ts_code)}</td>
      <td><strong>${escapeHtml(row.name || "—")}</strong></td>
      <td>${escapeHtml(row.industry || "—")}</td>
      <td class="mono ${signClass(row.pct_chg)}">${formatNumber(row.close)}</td>
      <td class="mono ${signClass(row.pct_chg)}">${pctText(row.pct_chg)}</td>
      <td class="mono muted">${row.last_date ? escapeHtml(formatDate(row.last_date)) : "—"}</td>
      <td><button type="button" class="button watch-remove" data-act="remove" aria-label="移除自选">移除</button></td>
    </tr>`).join("");
  rowsBody.querySelectorAll(".watch-row").forEach((tr) => {
    tr.addEventListener("click", (event) => {
      if (event.target.closest("[data-act='remove']")) return;
      location.href = `p6_chart.html?code=${encodeURIComponent(tr.dataset.code)}`;
    });
  });
  rowsBody.querySelectorAll("[data-act='remove']").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      await removeWatch(btn.closest("tr").dataset.code);
    });
  });
}

async function removeWatch(code) {
  clearError();
  try {
    await request(`/api/watchlist/${encodeURIComponent(code)}`, { method: "DELETE" });
    await loadWatchlist();
  } catch (error) {
    showError(error);
  }
}

async function addWatchFromInput() {
  const raw = addInput.value.trim();
  if (!raw) return;
  clearError();
  try {
    const items = (await query("/api/kline/search", { q: raw, limit: 5 })).items || [];
    const upper = raw.toUpperCase();
    const exact = items.find((item) => item.ts_code === upper || item.symbol === raw);
    const target = exact || items[0];
    if (!target) throw new Error(`没有找到 "${raw}" 对应的股票`);
    await request("/api/watchlist", { method: "POST", body: JSON.stringify({ ts_code: target.ts_code }) });
    addInput.value = "";
    await loadWatchlist();
  } catch (error) {
    showError(error);
  }
}

searchInput.addEventListener("input", renderRows);
industrySelect.addEventListener("change", renderRows);
refreshBtn.addEventListener("click", loadWatchlist);
addBtn.addEventListener("click", addWatchFromInput);
addInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); addWatchFromInput(); }
});

loadWatchlist();
