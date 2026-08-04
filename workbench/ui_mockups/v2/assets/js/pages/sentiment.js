import { request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("sentiment");

async function load() {
  clearError(); setLoading(true);
  try {
    const data = await request("/api/sentiment");
    renderMarketStage(data.market_stage || {});
    document.querySelector("#industry-rows").innerHTML = (data.industries || []).map((item, index) => `
      <tr><td class="mono">${index + 1}</td><td>${escapeHtml(item.industry || item.name || "未知")}</td><td class="mono">${formatNumber(item.heat ?? item.score ?? item.avg_pct, 2)}</td><td class="mono">${formatPercent(item.up_ratio)}</td></tr>`).join("") || `<tr><td colspan="4"><div class="empty-state">暂无行业热度数据</div></td></tr>`;
    document.querySelector("#money-classes").innerHTML = Object.entries(data.money_classes || {}).map(([name, count]) => `<div class="kv"><span>${escapeHtml(name)}</span><span>${formatNumber(count, 0)}</span></div>`).join("");
    renderNewsSentiment(data.news_sentiment || {});
    renderIndustryMoneyflow(data.industry_moneyflow || {});
    setStatus(`情绪截面 ${data.as_of}`, "ready");
  } catch (error) { showError(error); } finally { setLoading(false); }
}

document.querySelector("#refresh")?.addEventListener("click", load);
document.querySelector("#mf-refresh")?.addEventListener("click", load);
document.querySelector("#mf-filter")?.addEventListener("input", renderIndustryMoneyflowRows);
load();

// 市场结构:算不出就显示"算不出"并给出原因,不要退成"结构偏弱"那一档
function renderMarketStage(stage) {
  const labelEl = document.querySelector("#stage-label");
  const ratioEl = document.querySelector("#stage-ratio");
  const noteEl = document.querySelector("#stage-note");
  const available = stage.availability !== "unavailable" && stage.label != null;
  labelEl.textContent = available ? stage.label : "算不出";
  ratioEl.textContent = formatPercent(stage.passed_ratio);
  if (!noteEl) return;
  noteEl.textContent = available
    ? `由最新候选池门槛通过率判断 · 样本 ${formatNumber(stage.sample_count, 0)} 只`
    : stage.reason || "最新扫描没有明细行,判不出市场结构";
}

let industryMoneyflow = { items: [] };

// 涨红跌绿:正数红、负数绿、0 或空无色
function signClass(value) {
  if (value == null || Number.isNaN(Number(value)) || Number(value) === 0) return "";
  return Number(value) > 0 ? "up" : "down";
}

// 结构判断算不出时显示"算不出"并给原因，不要留空 —— 空白会被当成"结构偏弱"之外的
// 某种正常状态，而实际上是这一批扫描没有明细行、判不出来。
function renderMarketStage(stage) {
  const labelEl = document.querySelector("#stage-label");
  const ratioEl = document.querySelector("#stage-ratio");
  const noteEl = document.querySelector("#stage-note");
  const unavailable = stage.availability === "unavailable" || stage.label == null;
  if (labelEl) labelEl.textContent = unavailable ? "算不出" : stage.label;
  if (ratioEl) ratioEl.textContent = formatPercent(stage.passed_ratio);
  if (noteEl) {
    noteEl.textContent = unavailable
      ? stage.reason || "最新扫描没有明细行，判不出市场结构"
      : `由最新候选池门槛通过率判断 · 样本 ${formatNumber(stage.sample_count, 0)} 只`;
  }
}

function renderIndustryMoneyflow(mf) {
  industryMoneyflow = mf || { items: [] };
  renderIndustryMoneyflowRows();
}

function renderIndustryMoneyflowRows() {
  const rowsEl = document.querySelector("#industry-mf-rows");
  const noteEl = document.querySelector("#mf-industry-note");
  if (!rowsEl) return;
  const mf = industryMoneyflow;
  const items = mf.items || [];
  if (mf.availability !== "available" || !items.length) {
    rowsEl.innerHTML = `<tr><td colspan="6"><div class="empty-state">暂无行业资金流数据${mf.reason ? `（${escapeHtml(mf.reason)}）` : ""}</div></td></tr>`;
    if (noteEl) noteEl.textContent = "数据尚未采集或最新交易日无记录";
    return;
  }
  const range = mf.date_range || [];
  const rangeText = range.length === 2 ? `${formatDate(range[0])} ~ ${formatDate(range[1])}` : "—";
  if (noteEl) noteEl.textContent = `截至 ${formatDate(mf.as_of)} · 覆盖 ${formatNumber(mf.stock_count, 0)} 只股票 · 数据区间 ${rangeText} · 净流入红涨绿跌`;
  const keyword = (document.querySelector("#mf-filter")?.value || "").trim().toLowerCase();
  const rows = items.filter((item) => {
    if (!keyword) return true;
    return String(item.industry || "未知").toLowerCase().includes(keyword);
  });
  rowsEl.innerHTML = rows.map((item, index) => {
    const net = item.net_mf_amount;
    const lg = (item.buy_lg_amount ?? 0) - (item.sell_lg_amount ?? 0);
    const elg = (item.buy_elg_amount ?? 0) - (item.sell_elg_amount ?? 0);
    return `<tr>
      <td class="mono">${index + 1}</td>
      <td>${escapeHtml(item.industry || "未知")}</td>
      <td class="mono ${signClass(net)}">${formatNumber(net, 0)}</td>
      <td class="mono ${signClass(lg)}">${formatNumber(lg, 0)}</td>
      <td class="mono ${signClass(elg)}">${formatNumber(elg, 0)}</td>
      <td class="mono">${formatNumber(item.stock_count, 0)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6"><div class="empty-state">没有匹配的行业</div></td></tr>`;
}

function renderNewsSentiment(news) {
  const stateEl = document.querySelector("#community-state");
  const reasonEl = document.querySelector("#community-reason");
  if (!stateEl || !reasonEl) return;
  if (news.availability === "available") {
    const counts = news.counts || {};
    stateEl.innerHTML = statusTag("有数据", "good");
    reasonEl.innerHTML = `情绪判定为规则推断，未经人工核验（unverified）。积极 <strong>${formatNumber(counts.positive, 0)}</strong> · 消极 <strong>${formatNumber(counts.negative, 0)}</strong> · 中性 <strong>${formatNumber(counts.neutral, 0)}</strong> · 未判定 <strong>${formatNumber(counts.undecided, 0)}</strong>，共 ${formatNumber(news.sample_count, 0)} 条样本`;
  } else if (news.missing_reason === "no_source_registered") {
    stateEl.innerHTML = statusTag("未配置", "pending");
    reasonEl.textContent = news.detail || news.missing_reason || "暂无舆情数据";
  } else {
    stateEl.innerHTML = statusTag("暂无数据", "pending");
    reasonEl.textContent = news.detail || news.missing_reason || "暂无舆情数据";
  }
}
