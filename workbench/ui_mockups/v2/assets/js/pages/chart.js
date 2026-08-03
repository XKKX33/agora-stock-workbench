// p6_chart.js · 行情K线页控制器
// 数据全部来自真实 API：/api/kline/search（搜索）与 /api/kline/{ts_code}（K线详情）
import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber } from "/assets/js/format.js";

initShell("chart");

// A股习惯：涨=红、跌=绿（页内自定义，不沿用 theme 的 positive/negative）
const UP = "#e05a5a";
const DOWN = "#3da678";

let chart = null;
let bars = [];
let currentCode = new URLSearchParams(location.search).get("code") || "";
let watchItems = [];
let watchCodes = new Set();
let searchSeq = 0;
let suggestTimer = null;
let activeIndex = -1;
let sub = "macd";
let zoomStart = null;
let zoomEnd = null;
const overlay = { ma: true, boll: false };

const searchInput = document.querySelector("#kline-search");
const suggestBox = document.querySelector("#kline-suggest");
const chartWrap = document.querySelector("#kline-chart");
const chartHolder = document.querySelector(".chart-wrap");
const chartEmpty = document.querySelector("#chart-empty");
const moneyBody = document.querySelector("#moneyflow-rows");
const stockInfo = document.querySelector("#stock-info");
const infoSub = document.querySelector("#info-sub");
const chartSub = document.querySelector("#chart-sub");
const watchRows = document.querySelector("#watch-rows");
const watchSearch = document.querySelector("#watch-search");
const watchIndustry = document.querySelector("#watch-industry");
const watchAddInput = document.querySelector("#watch-add-code");
const watchAddBtn = document.querySelector("#watch-add-btn");
const watchRefreshBtn = document.querySelector("#watch-refresh");

/* ---------- 通用小工具 ---------- */

function setLoadingUI(active) {
  document.body.classList.toggle("loading", active);
}

// 涨红跌绿：正数红、负数绿、0 或空无色
function pctClass(value) {
  if (value == null || Number.isNaN(Number(value)) || Number(value) === 0) return "";
  return Number(value) > 0 ? "up" : "down";
}

function pctText(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const num = Number(value);
  return `${num > 0 ? "+" : ""}${formatNumber(num)}%`;
}

/* ---------- 顶部搜索（250ms 防抖） ---------- */

function hideSuggest() {
  searchSeq++;
  suggestBox.hidden = true;
  searchInput.setAttribute("aria-expanded", "false");
}

async function runSearch(q) {
  const seq = ++searchSeq;
  try {
    clearError();
    const data = await query("/api/kline/search", { q, limit: 20 });
    if (seq !== searchSeq) return;
    renderSuggest(data.items || []);
  } catch (error) {
    if (seq !== searchSeq) return;
    showError(error);
    hideSuggest();
  }
}

function renderSuggest(items) {
  activeIndex = -1;
  if (!items.length) {
    suggestBox.innerHTML = `<div class="suggest-empty">没有匹配的股票，换个关键字试试</div>`;
    suggestBox.hidden = false;
    searchInput.setAttribute("aria-expanded", "true");
    return;
  }
  suggestBox.innerHTML = items.map((item, index) => `
    <button type="button" class="suggest-item" role="option" data-code="${escapeHtml(item.ts_code)}" data-index="${index}">
      <span class="suggest-main">
        <strong>${escapeHtml(item.name)}</strong>
        <span class="mono muted">${escapeHtml(item.ts_code)}</span>
        <span class="muted">${escapeHtml(item.industry || "—")}</span>
      </span>
      <span class="suggest-meta">
        <span class="mono ${pctClass(item.pct_chg)}">${formatNumber(item.close)}</span>
        <span class="mono ${pctClass(item.pct_chg)}">${pctText(item.pct_chg)}</span>
        <span class="muted">${item.last_date ? escapeHtml(formatDate(item.last_date)) : ""}</span>
      </span>
    </button>`).join("");
  suggestBox.hidden = false;
  searchInput.setAttribute("aria-expanded", "true");
  suggestBox.querySelectorAll(".suggest-item").forEach((el) => {
    el.addEventListener("click", () => selectStock(el.dataset.code));
  });
}

function moveActive(step) {
  const items = [...suggestBox.querySelectorAll(".suggest-item")];
  if (!items.length) return;
  activeIndex = (activeIndex + step + items.length) % items.length;
  items.forEach((el, index) => el.classList.toggle("active", index === activeIndex));
  items[activeIndex].scrollIntoView({ block: "nearest" });
}

function selectStock(code) {
  clearTimeout(suggestTimer);
  searchInput.value = code;
  hideSuggest();
  searchInput.blur();
  loadDetail(code);
}

searchInput.addEventListener("input", () => {
  clearTimeout(suggestTimer);
  const q = searchInput.value.trim();
  if (!q) {
    hideSuggest();
    return;
  }
  suggestTimer = setTimeout(() => runSearch(q), 250);
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const item = suggestBox.querySelector(".suggest-item.active") || suggestBox.querySelector(".suggest-item");
    if (item) selectStock(item.dataset.code);
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    moveActive(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Escape") {
    hideSuggest();
  }
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".search-box")) hideSuggest();
});

/* ---------- 加载详情 ---------- */

async function loadDetail(code) {
  currentCode = code;
  history.replaceState(null, "", `?code=${encodeURIComponent(code)}`);
  searchInput.value = code;
  clearError();
  setLoadingUI(true);
  try {
    const data = await query(`/api/kline/${encodeURIComponent(code)}`, { days: 250 });
    bars = data.bars || [];
    zoomStart = null;
    zoomEnd = null;
    renderInfo(data);
    renderChart();
    renderMoneyFlow(data.moneyflow || []);
    infoSub.textContent = data.list_date
      ? `${data.market || ""} · 上市日 ${formatDate(data.list_date)}`.trim()
      : (data.market || "—");
    refreshWatchStar();
    setStatus(`${data.name} 行情已加载`, "ready");
  } catch (error) {
    showError(error);
  } finally {
    setLoadingUI(false);
  }
}

function renderInfo(data) {
  const q = data.quote || {};
  const cells = [
    ["最新价", formatNumber(q.close), pctClass(q.pct_chg)],
    ["涨跌幅", pctText(q.pct_chg), pctClass(q.pct_chg)],
    ["今开", formatNumber(q.open), ""],
    ["最高", formatNumber(q.high), ""],
    ["最低", formatNumber(q.low), ""],
    ["成交量", formatNumber(q.vol, 0), ""],
    ["成交额", formatNumber(q.amount, 0), ""],
    ["换手率", q.turnover_rate == null ? "—" : `${formatNumber(q.turnover_rate)}%`, ""],
    ["量比", formatNumber(q.volume_ratio), ""],
    ["总市值", formatNumber(q.total_mv, 0), ""],
    ["流通市值", formatNumber(q.circ_mv, 0), ""],
  ];
  stockInfo.innerHTML = `
    <div class="stock-head">
      <div class="stock-name">${escapeHtml(data.name)}</div>
      <div class="stock-meta mono">${escapeHtml(data.ts_code)}</div>
      <div class="stock-tags">
        ${data.industry ? `<span class="tag">${escapeHtml(data.industry)}</span>` : ""}
        ${data.market ? `<span class="tag">${escapeHtml(data.market)}</span>` : ""}
        ${data.list_date ? `<span class="tag">上市 ${escapeHtml(formatDate(data.list_date))}</span>` : ""}
      </div>
    </div>
    <div class="quote-grid">
      ${cells.map(([label, value, cls]) => `
        <div class="metric-cell"><span class="metric-label">${label}</span><span class="metric-value mono ${cls}">${value}</span></div>`).join("")}
    </div>`;
}

/* ---------- K线图（ECharts） ---------- */

function renderChart() {
  if (!bars.length) {
    chartHolder.classList.remove("has-data");
    chartEmpty.innerHTML = "该股票暂无K线数据";
    chartSub.textContent = "暂无K线数据";
    return;
  }
  if (!chart) chart = echarts.init(chartWrap);
  chartHolder.classList.add("has-data");
  chart.setOption(buildOption(), true);
  chartSub.textContent = `${bars.length} 个交易日 · 十字光标查看当日指标`;
}

function line(name, field, color, axisIndex, width = 1.1) {
  return {
    name,
    type: "line",
    data: bars.map((b) => b[field]),
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    symbol: "none",
    smooth: false,
    z: 3,
    lineStyle: { width, color },
    itemStyle: { color },
    emphasis: { disabled: true },
  };
}

function buildOption() {
  const dates = bars.map((b) => formatDate(b.trade_date));
  // ECharts 蜡烛图数据顺序：开、收、低、高
  const kData = bars.map((b) => [b.open, b.close, b.low, b.high]);
  const volData = bars.map((b) => ({
    value: b.vol,
    itemStyle: { color: b.close >= b.open ? UP : DOWN },
  }));
  const macdData = bars.map((b) => ({
    value: b.macd,
    itemStyle: { color: Number(b.macd) >= 0 ? UP : DOWN },
  }));
  const defaultStart = bars.length > 100 ? Math.round((1 - 100 / bars.length) * 100) : 0;
  const start = zoomStart == null ? defaultStart : zoomStart;
  const end = zoomEnd == null ? 100 : zoomEnd;

  const series = [
    {
      name: "日K",
      type: "candlestick",
      data: kData,
      xAxisIndex: 0,
      yAxisIndex: 0,
      itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
      z: 2,
    },
  ];
  if (overlay.ma) {
    series.push(
      line("MA5", "ma5", "#e8c46a", 0),
      line("MA10", "ma10", "#7db8e8", 0),
      line("MA20", "ma20", "#c792ea", 0),
      line("MA60", "ma60", "#8bd0b0", 0),
    );
  }
  if (overlay.boll) {
    series.push(
      line("BOLL上轨", "boll_upper", "#7db8e8", 0),
      line("BOLL中轨", "boll_mid", "#e8c46a", 0),
      line("BOLL下轨", "boll_lower", "#c792ea", 0),
    );
  }
  series.push({ name: "成交量", type: "bar", data: volData, xAxisIndex: 1, yAxisIndex: 1, barWidth: "60%" });
  if (sub === "macd") {
    series.push({ name: "MACD", type: "bar", data: macdData, xAxisIndex: 2, yAxisIndex: 2, barWidth: "55%" });
    series.push(line("DIF", "dif", "#e8c46a", 2), line("DEA", "dea", "#7db8e8", 2));
  } else if (sub === "kdj") {
    series.push(line("K", "k", "#e8c46a", 2), line("D", "d", "#7db8e8", 2), line("J", "j", "#c792ea", 2));
  } else if (sub === "rsi") {
    series.push(line("RSI6", "rsi6", "#e8c46a", 2), line("RSI12", "rsi12", "#7db8e8", 2), line("RSI24", "rsi24", "#c792ea", 2));
  }

  return {
    backgroundColor: "transparent",
    animationDuration: 240,
    tooltip: {
      trigger: "axis",
      axisPointer: {
        type: "cross",
        crossStyle: { color: "#5f8fc9" },
        label: { backgroundColor: "#16233a", borderColor: "#30415a", color: "#f4f7fb" },
      },
      formatter: tooltipFormatter,
      backgroundColor: "rgba(11, 18, 29, .96)",
      borderColor: "#334155",
      textStyle: { color: "#f4f7fb", fontSize: 11 },
      confine: true,
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 58, right: 16, top: 14, height: "44%" },
      { left: 58, right: 16, top: "55%", height: "13%" },
      { left: 58, right: 16, top: "72%", height: "16%" },
    ],
    xAxis: [0, 1, 2].map((i) => ({
      type: "category",
      data: dates,
      gridIndex: i,
      boundaryGap: true,
      axisLine: { lineStyle: { color: "#263449" } },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { show: false },
    })),
    yAxis: [
      { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: "#182334" } }, axisLabel: { color: "#718096", fontSize: 10 } },
      { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { color: "#718096", fontSize: 10 } },
      { gridIndex: 2, scale: true, splitLine: { lineStyle: { color: "#182334" } }, axisLabel: { color: "#718096", fontSize: 10 } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2], start, end },
      {
        type: "slider",
        xAxisIndex: [0, 1, 2],
        bottom: 4,
        height: 18,
        start,
        end,
        showDetail: false,
        borderColor: "#263449",
        backgroundColor: "#0b121d",
        fillerColor: "rgba(95, 143, 201, .25)",
        handleStyle: { color: "#5f8fc9" },
        textStyle: { color: "#8d9aab", fontSize: 9 },
        dataBackground: { lineStyle: { color: "#30415a" }, areaStyle: { color: "#16233a" } },
      },
    ],
    series,
  };
}

function tooltipFormatter(params) {
  const index = params?.[0]?.dataIndex ?? -1;
  const bar = bars[index];
  if (!bar) return "";
  const cls = pctClass(bar.pct_chg);
  const rows = [
    tipRow("开盘", formatNumber(bar.open)),
    tipRow("最高", formatNumber(bar.high)),
    tipRow("最低", formatNumber(bar.low)),
    tipRow("收盘", formatNumber(bar.close), cls),
    tipRow("涨跌幅", pctText(bar.pct_chg), cls),
    tipRow("成交量", formatNumber(bar.vol, 0)),
    tipRow("成交额", formatNumber(bar.amount, 0)),
  ];
  [
    ["MA5", bar.ma5], ["MA10", bar.ma10], ["MA20", bar.ma20], ["MA60", bar.ma60],
    ["DIF", bar.dif], ["DEA", bar.dea], ["MACD", bar.macd],
    ["K", bar.k], ["D", bar.d], ["J", bar.j],
    ["RSI6", bar.rsi6], ["RSI12", bar.rsi12], ["RSI24", bar.rsi24],
    ["BOLL上轨", bar.boll_upper], ["BOLL中轨", bar.boll_mid], ["BOLL下轨", bar.boll_lower],
  ].forEach(([name, value]) => {
    if (value != null && !Number.isNaN(Number(value))) rows.push(tipRow(name, formatNumber(value)));
  });
  return `<div class="tip-title">${escapeHtml(formatDate(bar.trade_date))}</div>${rows.join("")}`;
}

function tipRow(label, value, cls = "") {
  return `<div class="tip-row"><span class="tip-label">${label}</span><span class="mono ${cls}">${value}</span></div>`;
}

/* ---------- 资金流向（最近 10 条） ---------- */

function renderMoneyFlow(rows) {
  if (!rows.length) {
    moneyBody.innerHTML = `<tr><td colspan="6"><div class="empty-state">暂无资金流数据</div></td></tr>`;
    return;
  }
  const recent = rows.slice(-10).reverse();
  moneyBody.innerHTML = recent.map((row) => `
    <tr>
      <td class="mono">${escapeHtml(formatDate(row.trade_date))}</td>
      <td class="mono ${pctClass(row.net_mf_amount)}">${formatNumber(row.net_mf_amount, 0)}</td>
      <td class="mono">${formatNumber(row.buy_lg_amount, 0)}</td>
      <td class="mono">${formatNumber(row.sell_lg_amount, 0)}</td>
      <td class="mono">${formatNumber(row.buy_elg_amount, 0)}</td>
      <td class="mono">${formatNumber(row.sell_elg_amount, 0)}</td>
    </tr>`).join("");
}

/* ---------- 指标切换（MA/BOLL 叠加主图，MACD/KDJ/RSI 切换副图） ---------- */

function refreshIndicatorButtons() {
  document.querySelectorAll(".indicator-btn").forEach((el) => {
    const key = el.dataset.ind;
    const active = key === "ma" || key === "boll" ? overlay[key] : sub === key;
    el.classList.toggle("active", active);
    if (key === "ma" || key === "boll") el.setAttribute("aria-pressed", String(active));
  });
}

function refreshChart() {
  if (!chart || !bars.length) return;
  // 切换指标时保留当前缩放范围，避免视图跳回开头
  const zoom = chart.getOption().dataZoom?.[0] || {};
  if (typeof zoom.start === "number") zoomStart = zoom.start;
  if (typeof zoom.end === "number") zoomEnd = zoom.end;
  chart.setOption(buildOption(), true);
}

document.querySelector("#indicator-group").addEventListener("click", (event) => {
  const btn = event.target.closest(".indicator-btn");
  if (!btn) return;
  const key = btn.dataset.ind;
  if (key === "ma" || key === "boll") {
    overlay[key] = !overlay[key];
    refreshIndicatorButtons();
    refreshChart();
  } else if (sub !== key) {
    sub = key;
    refreshIndicatorButtons();
    refreshChart();
  }
});

window.addEventListener("resize", () => chart?.resize());

/* ---------- 自选股 ---------- */

async function loadWatchlist() {
  clearError();
  try {
    const data = await query("/api/watchlist", { per_page: 200, sort: "sort_order", order: "asc" });
    watchItems = data.items || [];
    watchCodes = new Set(watchItems.map((item) => item.ts_code));
    renderWatchFilters();
    renderWatchRows();
  } catch (error) {
    showError(error);
  }
}

function renderWatchFilters() {
  const current = watchIndustry.value;
  const industries = [...new Set(watchItems.map((item) => item.industry).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
  watchIndustry.innerHTML = `<option value="">全部行业</option>` + industries
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  watchIndustry.value = industries.includes(current) ? current : "";
}

function renderWatchRows() {
  const keyword = (watchSearch.value || "").trim().toLowerCase();
  const industry = watchIndustry.value;
  const rows = watchItems.filter((item) => {
    const hitKeyword = !keyword
      || `${item.ts_code} ${item.symbol || ""} ${item.name || ""}`.toLowerCase().includes(keyword);
    const hitIndustry = !industry || item.industry === industry;
    return hitKeyword && hitIndustry;
  });
  if (!rows.length) {
    watchRows.innerHTML = `<tr class="watch-empty"><td colspan="7"><div class="empty-state">${watchItems.length ? "暂无匹配的自选股，调整筛选条件看看" : "还没有自选股：搜索股票后点击左侧「加入自选」，或在上方输入代码添加"}</div></td></tr>`;
    refreshWatchStar();
    return;
  }
  watchRows.innerHTML = rows.map((row) => `
    <tr class="watch-row" data-code="${escapeHtml(row.ts_code)}">
      <td class="mono">${escapeHtml(row.ts_code)}</td>
      <td>${escapeHtml(row.name || "—")}</td>
      <td>${escapeHtml(row.industry || "—")}</td>
      <td class="mono ${pctClass(row.pct_chg)}">${formatNumber(row.close)}</td>
      <td class="mono ${pctClass(row.pct_chg)}">${pctText(row.pct_chg)}</td>
      <td class="mono muted">${row.last_date ? escapeHtml(formatDate(row.last_date)) : "—"}</td>
      <td><button type="button" class="button watch-remove" data-act="remove" aria-label="移除自选">移除</button></td>
    </tr>`).join("");
  watchRows.querySelectorAll(".watch-row").forEach((tr) => {
    tr.addEventListener("click", (event) => {
      if (event.target.closest("[data-act='remove']")) return;
      loadDetail(tr.dataset.code);
    });
  });
  watchRows.querySelectorAll("[data-act='remove']").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleWatch(btn.closest("tr").dataset.code);
    });
  });
  refreshWatchStar();
}

function refreshWatchStar() {
  const host = document.querySelector("#watch-toggle");
  if (!host) return;
  if (!currentCode) { host.innerHTML = ""; return; }
  const inList = watchCodes.has(currentCode);
  host.innerHTML = inList
    ? `<button type="button" class="button watch-star" data-act="toggle"><span class="star">★</span> 已自选</button>`
    : `<button type="button" class="button primary watch-star" data-act="toggle"><span class="star">☆</span> 加入自选</button>`;
  host.querySelector("[data-act='toggle']").addEventListener("click", () => toggleWatch(currentCode));
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
  const raw = watchAddInput.value.trim();
  if (!raw) return;
  clearError();
  try {
    const items = (await query("/api/kline/search", { q: raw, limit: 5 })).items || [];
    const upper = raw.toUpperCase();
    const exact = items.find((item) => item.ts_code === upper || item.symbol === raw);
    const target = exact || items[0];
    if (!target) throw new Error(`没有找到 "${raw}" 对应的股票`);
    await request("/api/watchlist", { method: "POST", body: JSON.stringify({ ts_code: target.ts_code }) });
    watchAddInput.value = "";
    await loadWatchlist();
  } catch (error) {
    showError(error);
  }
}

watchSearch.addEventListener("input", renderWatchRows);
watchIndustry.addEventListener("change", renderWatchRows);
watchRefreshBtn.addEventListener("click", loadWatchlist);
watchAddBtn.addEventListener("click", addWatchFromInput);
watchAddInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); addWatchFromInput(); }
});

/* ---------- 启动 ---------- */

refreshIndicatorButtons();
loadWatchlist();
if (currentCode) {
  searchInput.value = currentCode;
  loadDetail(currentCode);
}