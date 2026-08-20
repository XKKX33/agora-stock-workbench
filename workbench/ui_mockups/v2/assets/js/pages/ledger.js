import { query } from "/assets/js/api.js";
import { clearError, getWorkContext, initShell, setLoading, setStatus, showError, workContextParams } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("ledger");

const GROUP_LABELS = { rule: "规则", ai: "AI", hybrid: "混合", benchmark: "基准" };
// experiment_returns 的 entry_status / status / reason 全量中文映射。
// 算不出的格子只翻译原因，绝不退化成 0。
const STATUS_LABELS = {
  filled: "已成交",
  pending_entry: "待成交",
  entry_unavailable: "无法成交",
  entry_bar_missing: "当日无行情",
  invalid_open: "开盘价异常",
  limit_price_missing: "缺涨跌停价",
  limit_up_locked: "涨停封板",
  future_not_reached: "未来未到",
  future_not_visible: "超出可见窗口",
  calendar_missing: "缺交易日历",
  target_bar_missing: "卖出日无行情",
};
const HORIZON_LABELS = {
  t1_close: "T+1 收盘",
  t2_open: "T+2 开盘",
  t3_open: "T+3 开盘",
  t4_open: "T+4 开盘",
  t5_open: "T+5 开盘",
  t6_open: "T+6 开盘",
  t7_open: "T+7 开盘",
  t8_open: "T+8 开盘",
  t9_open: "T+9 开盘",
  t10_open: "T+10 开盘",
};
const SESSION_LABELS = { open: "开盘", close: "收盘" };
// 明细表只展示四个关键期限，完整十个期限交给收益带。
const ROW_HORIZONS = ["t1_close", "t3_open", "t5_open", "t10_open"];
// 分组卡统一用 T+1 收盘做四组横向对比。
const CARD_HORIZON = "t1_close";
const COLUMN_COUNT = 12;
const PAGE_SIZE = 200;
let currentPage = 1;

function apiDate(value) { return value ? value.replaceAll("-", "") : ""; }

function filters() {
  return {
    ...workContextParams(),
    as_of: apiDate(document.querySelector("#as-of").value) || getWorkContext().as_of,
    group: document.querySelector("#group").value,
    ts_code: document.querySelector("#ts-code").value.trim().toUpperCase(),
    entry_status: document.querySelector("#entry-status").value,
    page: currentPage,
    per_page: PAGE_SIZE,
  };
}

// /api/returns/summary 不分页，且实验组的参数名是 group_name。
function summaryFilters() {
  const { page, per_page, group, ...activeFilters } = filters();
  return { ...activeFilters, group_name: group };
}

async function load() {
  clearError(); setLoading(true);
  try {
    const [ledger, returns] = await Promise.all([
      query("/api/experiments", filters()),
      query("/api/returns/summary", summaryFilters()),
    ]);
    const groups = returns?.groups || {};
    renderGroupSummary(groups, ledger.total);
    renderReturnsSummary(groups);
    renderRows(ledger.items || []);
    renderPagination(ledger);
    setStatus(`${ledger.total} 条实验记录`, "ready");
  } catch (error) { showError(error); } finally { setLoading(false); }
}

function statusText(value) {
  if (!value) return "";
  return STATUS_LABELS[value] || String(value);
}

// 占比最大的状态，用来解释「为什么算不出」。
function dominantStatus(distribution) {
  const entries = Object.entries(distribution || {});
  if (!entries.length) return null;
  entries.sort((left, right) => Number(right[1]) - Number(left[1]));
  return { status: entries[0][0], count: Number(entries[0][1]) };
}

function mergeDistribution(entries) {
  const merged = {};
  entries.forEach((entry) => {
    Object.entries(entry.status_distribution || {}).forEach(([status, count]) => {
      merged[status] = (merged[status] || 0) + Number(count || 0);
    });
  });
  return merged;
}

// 四组一张卡：T+1 收盘均值、可测/计划样本、覆盖率；均值缺失时讲清原因。
function renderGroupSummary(groups, filteredTotal) {
  const total = document.querySelector("#ledger-total");
  if (total) total.textContent = `当前筛选 ${formatNumber(filteredTotal, 0)} 条`;
  const host = document.querySelector("#group-summary");
  if (!host) return;
  host.innerHTML = Object.entries(GROUP_LABELS).map(([name, label]) => {
    const stat = groups[name]?.[CARD_HORIZON] || null;
    const planned = Number(stat?.planned_count || 0);
    const measurable = Number(stat?.measurable_count || 0);
    const average = stat?.average ?? null;
    const coverage = stat?.coverage == null ? "覆盖率 —" : `覆盖率 ${formatPercent(stat.coverage)}`;
    const dominant = dominantStatus(stat?.status_distribution);
    let note = `可测 ${formatNumber(measurable, 0)} / 计划 ${formatNumber(planned, 0)} · ${coverage}`;
    let hint = `${label}组 ${HORIZON_LABELS[CARD_HORIZON]}：${note}`;
    if (average == null) {
      const cause = planned
        ? (dominant ? `${statusText(dominant.status)} 占 ${formatNumber(dominant.count, 0)}/${formatNumber(planned, 0)}` : "收益明细缺失")
        : "当前筛选没有样本";
      note = `${note} · ${cause}`;
      hint = `${label}组 ${HORIZON_LABELS[CARD_HORIZON]} 没有可测样本：${cause}`;
    }
    return `<div class="group-stat" title="${escapeHtml(hint)}"><span>${escapeHtml(label)} · ${HORIZON_LABELS[CARD_HORIZON]}</span><strong class="mono ${returnClass(average)}">${average == null ? "—" : formatPercent(average)}</strong><small>${escapeHtml(note)}</small></div>`;
  }).join("");
}

// 十个期限一条带：四组合并后的加权均值，按期限切。
function renderReturnsSummary(groups) {
  const host = document.querySelector("#returns-summary");
  if (!host) return;
  host.innerHTML = Object.entries(HORIZON_LABELS).map(([horizon, label]) => {
    const entries = Object.values(groups).map((group) => group?.[horizon]).filter(Boolean);
    const planned = entries.reduce((total, item) => total + Number(item.planned_count || 0), 0);
    const measurable = entries.reduce((total, item) => total + Number(item.measurable_count || 0), 0);
    const weighted = entries.reduce((total, item) => total + (item.average == null ? 0 : Number(item.average) * Number(item.measurable_count || 0)), 0);
    const average = measurable ? weighted / measurable : null;
    const dominant = dominantStatus(mergeDistribution(entries));
    let note = `可测 ${formatNumber(measurable, 0)} / 计划 ${formatNumber(planned, 0)}`;
    let hint = `${label}：四组合并加权均值，${note}`;
    if (average == null) {
      const cause = planned
        ? (dominant ? `${statusText(dominant.status)} 占 ${formatNumber(dominant.count, 0)}/${formatNumber(planned, 0)}` : "收益明细缺失")
        : "当前筛选没有样本";
      note = `${note} · ${cause}`;
      hint = `${label} 没有可测样本：${cause}`;
    }
    return `<div class="group-stat" title="${escapeHtml(hint)}"><span>${label}</span><strong class="mono ${returnClass(average)}">${average == null ? "—" : formatPercent(average)}</strong><small>${escapeHtml(note)}</small></div>`;
  }).join("");
}

function returnClass(value) {
  if (value == null || Number(value) === 0) return "";
  return Number(value) > 0 ? "return-up" : "return-down";
}

// status + reason 翻成一句中文，reason 与 status 不同才拼接。
function explainDetail(detail) {
  if (!detail) return "未计算收益";
  const status = statusText(detail.status);
  const reason = detail.reason ? statusText(detail.reason) : "";
  if (reason && reason !== status) return status ? `${status} · ${reason}` : reason;
  return status || reason || "待回填";
}

function sellHint(detail) {
  const session = SESSION_LABELS[detail?.sell_session] || detail?.sell_session || "";
  const date = detail?.sell_date ? formatDate(detail.sell_date) : "—";
  const price = detail?.sell_price == null ? "—" : formatNumber(detail.sell_price, 2);
  return `卖出 ${date} ${session} ${price}`;
}

function returnCell(item, horizon) {
  const detail = item.returns?.[horizon] || null;
  const value = detail?.gross_return ?? null;
  if (value == null) {
    return `<td class="mono muted" title="${escapeHtml(`${HORIZON_LABELS[horizon]}：${explainDetail(detail)}`)}">—</td>`;
  }
  return `<td class="mono ${returnClass(value)}" title="${escapeHtml(sellHint(detail))}">${formatPercent(value)}</td>`;
}

// 待处理原因：第一个非 filled 的期限，reason 优先、否则 status。
function pendingReason(item) {
  if (!item.entry_status) return "未计算收益";
  for (const horizon of Object.keys(HORIZON_LABELS)) {
    const detail = item.returns?.[horizon];
    if (!detail || detail.status === "filled") continue;
    return `${HORIZON_LABELS[horizon]}：${explainDetail(detail)}`;
  }
  return "";
}

function entryKind(status) {
  if (status === "filled") return "good";
  if (status === "entry_unavailable") return "bad";
  if (status === "pending_entry") return "pending";
  return "muted";
}

function renderRows(items) {
  let lastDate = null;
  const rows = [];
  items.forEach((item) => {
    if (item.as_of !== lastDate) {
      lastDate = item.as_of;
      rows.push(`<tr class="date-divider"><td colspan="${COLUMN_COUNT}"><span>${escapeHtml(formatDate(item.as_of))}</span><small>信号日</small></td></tr>`);
    }
    rows.push(`<tr>
      <td class="mono">${escapeHtml(formatDate(item.as_of))}</td>
      <td>${statusTag(GROUP_LABELS[item.group_name] || item.group_name, "active")}</td>
      <td class="mono">${escapeHtml(formatNumber(item.rank, 0))}</td>
      <td><strong>${escapeHtml(item.name || "—")}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td>
      <td class="mono">${escapeHtml(formatDate(item.entry_date))}</td>
      <td class="mono">${formatNumber(item.entry_price, 2)}</td>
      <td>${statusTag(STATUS_LABELS[item.entry_status] || "未计算", entryKind(item.entry_status))}</td>
      ${ROW_HORIZONS.map((horizon) => returnCell(item, horizon)).join("")}
      <td class="muted reason-cell">${escapeHtml(pendingReason(item)) || "—"}</td>
    </tr>`);
  });
  document.querySelector("#ledger-rows").innerHTML = rows.join("") || `<tr><td colspan="${COLUMN_COUNT}"><div class="empty-state">当前筛选没有实验记录</div></td></tr>`;
}

function renderPagination(ledger) {
  const totalPages = Math.max(1, Math.ceil(Number(ledger.total || 0) / PAGE_SIZE));
  currentPage = Math.min(Math.max(1, Number(ledger.page || currentPage)), totalPages);
  document.querySelector("#page-label").textContent = `第 ${currentPage} / ${totalPages} 页`;
  document.querySelector("#prev-page").disabled = currentPage <= 1;
  document.querySelector("#next-page").disabled = currentPage >= totalPages;
}

document.querySelector("#ledger-filters")?.addEventListener("submit", (event) => { event.preventDefault(); currentPage = 1; load(); });
document.querySelector("#clear-filters")?.addEventListener("click", () => { document.querySelector("#ledger-filters").reset(); currentPage = 1; load(); });
document.querySelector("#prev-page")?.addEventListener("click", () => { if (currentPage > 1) { currentPage -= 1; load(); } });
document.querySelector("#next-page")?.addEventListener("click", () => { currentPage += 1; load(); });
document.querySelector("#refresh")?.addEventListener("click", load);
load();
