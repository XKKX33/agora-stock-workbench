// p9_backtest.js · 组合回测页控制器
// 单策略详情来自 /api/backtest,多策略并排来自 /api/backtest/compare。
// 指标、口径判定、覆盖率全部由 engine/backtest 算好,页面只做展示与配色。
//
// 两条硬规则:
// 1. available === false 时绝不画曲线。此时把"为什么没有"放到横幅主位——
//    一条从 1.0 开始的平线看着像"策略没赚钱",而真实情况是"还没测得到"。
// 2. 成本、调仓口径、权重属于**假设**,单独一栏,不混进指标卡冒充事实。
import { query } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatNumber, formatPercent } from "/assets/js/format.js";

initShell("backtest");

let equityChart = null;
let compareChart = null;
let optionsReady = false;

const HORIZON_LABEL = { ret1: "T+1", ret3: "T+3", ret5: "T+5", ret10: "T+10" };

// missing_reason 是 engine 给的机器码,页面负责翻成"下一步该做什么"。
// 只显示原始码等于把排查工作又推回给用户。
const REASON_TEXT = {
  no_picks: ["台账里没有选股记录", "先在选股台跑一次扫描并记账，回测才有输入。"],
  no_picks_on_day: ["调仓日当天没有选股", "该截面的台账为空。"],
  no_measurable_period: [
    "有台账，但没有一期的收益回填完整",
    "retN 要等 T+N 个交易日之后才有值。等交易日走满，或在流程页跑一次回填。",
  ],
};

function reasonOf(code) {
  if (!code) return ["无法结算", ""];
  if (code.startsWith("column_missing:")) {
    const column = code.slice("column_missing:".length);
    return [`台账里没有 ${column} 列`, "该期限从未被回填过，换一个期限或先补齐回填。"];
  }
  return REASON_TEXT[code] || [code, ""];
}

function readControls() {
  const topK = Number(document.querySelector("#top-k").value) || 5;
  const rawCost = document.querySelector("#cost-bps").value;
  return {
    strategy: document.querySelector("#strategy").value,
    horizon: document.querySelector("#horizon").value || "ret5",
    top_k: topK,
    // 空串表示"用服务端默认",不在前端猜一个数写进请求
    cost_bps: rawCost === "" ? undefined : Number(rawCost),
  };
}

async function load() {
  clearError();
  setLoading(true);
  const params = readControls();
  try {
    const [single, compare] = await Promise.all([
      query("/api/backtest", params),
      query("/api/backtest/compare", { horizon: params.horizon, top_k: params.top_k, cost_bps: params.cost_bps }),
    ]);
    syncOptions(single, compare);
    renderNotice(single);
    renderMetrics(single);
    renderEquity(single);
    renderPeriods(single);
    renderAssumptions(single);
    renderCompare(compare);
    const coverage = single.coverage || {};
    setStatus(
      single.available
        ? `${HORIZON_LABEL[single.horizon] || single.horizon} · 已测 ${coverage.measured_periods} 期`
        : "回测无可用期次",
      single.available ? "ready" : "error",
    );
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

// 期限选项、默认成本、策略清单都由接口给出,页面不写死——写死一份就会和 engine 分叉。
function syncOptions(single, compare) {
  const horizonSelect = document.querySelector("#horizon");
  if (!optionsReady && Array.isArray(single.horizons)) {
    // 顺序直接用接口给的:engine/backtest.horizons() 按持仓天数排序。
    // 页面再排一遍就是把同一份知识写两处,迟早分叉。
    horizonSelect.innerHTML = single.horizons
      .map((h) => `<option value="${escapeHtml(h)}">${escapeHtml(HORIZON_LABEL[h] || h)} 调仓</option>`)
      .join("");
    horizonSelect.value = single.horizon;
    optionsReady = true;
  }
  const costInput = document.querySelector("#cost-bps");
  if (costInput.value === "" && single.default_cost_bps != null) {
    costInput.value = String(single.default_cost_bps);
    costInput.placeholder = String(single.default_cost_bps);
  }

  const select = document.querySelector("#strategy");
  const names = (compare.items || []).map((item) => item.strategy).filter(Boolean);
  const current = select.value;
  const wanted = ["", ...names];
  const existing = [...select.options].map((option) => option.value);
  if (String(existing) !== String(wanted)) {
    select.innerHTML = [
      `<option value="">全部策略</option>`,
      ...names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`),
    ].join("");
    select.value = names.includes(current) ? current : "";
  }
}

function renderNotice(payload) {
  const host = document.querySelector("#notice");
  if (payload.available) {
    host.hidden = true;
    return;
  }
  const [title, advice] = reasonOf(payload.missing_reason);
  const coverage = payload.coverage || {};
  host.hidden = false;
  host.innerHTML = `<strong>${escapeHtml(title)}</strong>
    ${escapeHtml(advice)}
    <div class="notice-detail">台账截面 ${formatNumber(coverage.available_days, 0)} · 应有期次 ${formatNumber(coverage.scheduled_periods, 0)} · 已测 ${formatNumber(coverage.measured_periods, 0)} · 跳过 ${formatNumber(coverage.skipped_periods, 0)}</div>`;
}

/* ---------- 指标卡 ---------- */

function renderMetrics(payload) {
  const m = payload.metrics || {};
  const drawdown = payload.drawdown || {};
  const peak = drawdown.peak_label && drawdown.trough_label
    ? `${drawdown.peak_label} → ${drawdown.trough_label}`
    : "峰谷未定位";
  const first = [
    card("净收益", m.total_return, formatPercent, "已扣换手成本", signOf(m.total_return)),
    card("毛收益", m.gross_total_return, formatPercent, "未扣成本，用于复核", signOf(m.gross_total_return)),
    card("年化", m.cagr, formatPercent, m.span_days ? `跨度 ${m.span_days} 天` : "跨度不足 30 天不算", signOf(m.cagr)),
    card("最大回撤", m.max_drawdown, formatPercent, peak, m.max_drawdown > 0 ? "negative" : "muted"),
  ];
  const second = [
    card("夏普", m.sharpe, (v) => formatNumber(v, 2), "按调仓频率年化", signOf(m.sharpe)),
    card("胜率", m.win_rate, formatPercent, `${formatNumber(m.n_periods, 0)} 期中盈利占比`, ""),
    card("平均换手", m.avg_turnover, formatPercent, "首期建仓记 100%", ""),
    card("盈亏比", m.profit_factor, (v) => formatNumber(v, 2), "无亏损期时算不出", signOf(m.profit_factor ? m.profit_factor - 1 : null)),
  ];
  document.querySelector("#metric-cards").innerHTML = first.join("");
  document.querySelector("#metric-cards-2").innerHTML = second.join("");
}

// 缺失一律显示"算不出"并转灰。显示 0 会让"没算出来"和"算出来是 0"混成一件事。
function card(label, value, fmt, note, tone) {
  const empty = value === null || value === undefined;
  return `<article class="panel metric">
    <span class="metric-label">${escapeHtml(label)}</span>
    <strong class="metric-value ${empty ? "muted" : tone}">${escapeHtml(empty ? "算不出" : fmt(value))}</strong>
    <span class="metric-note">${escapeHtml(note)}</span>
  </article>`;
}

function signOf(value) {
  if (value === null || value === undefined) return "";
  return Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "muted";
}

/* ---------- 净值曲线 ---------- */

function renderEquity(payload) {
  const host = document.querySelector("#equity-chart");
  const meta = document.querySelector("#curve-meta");
  const hint = document.querySelector("#gap-hint");
  const coverage = payload.coverage || {};

  hint.hidden = !coverage.has_interior_gap;
  if (coverage.has_interior_gap) {
    hint.textContent = `中间有 ${formatNumber(coverage.skipped_periods, 0)} 期被跳过。这条曲线是"已测期次的连乘"，连乘时被跳过的那期等于按 0 收益处理，不是完整日历净值。`;
  }

  if (!payload.available) {
    if (equityChart) { equityChart.dispose(); equityChart = null; }
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">没有可测期次，不画曲线</div>`;
    meta.textContent = "";
    return;
  }

  const curve = payload.equity_curve || [];
  meta.innerHTML = `${escapeHtml(payload.strategy)} · ${escapeHtml(HORIZON_LABEL[payload.horizon] || payload.horizon)}<br>持仓 ${formatNumber(payload.top_k, 0)} · 成本 ${formatNumber(payload.assumptions?.cost_bps, 1)}bp`;

  if (typeof echarts === "undefined") {
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">图表库未加载</div>`;
    return;
  }
  if (!equityChart) equityChart = echarts.init(host);

  const drawdown = payload.drawdown || {};
  const markArea = drawdown.peak_label && drawdown.trough_label
    ? {
        silent: true,
        itemStyle: { color: "rgba(200,93,103,.12)" },
        label: { show: true, position: "top", color: "#e0959e", fontSize: 10, formatter: `回撤 ${formatPercent(drawdown.max)}` },
        data: [[{ xAxis: drawdown.peak_label }, { xAxis: drawdown.trough_label }]],
      }
    : undefined;

  equityChart.setOption({
    backgroundColor: "transparent",
    grid: { top: 26, right: 18, bottom: 30, left: 52 },
    legend: {
      data: ["净值（扣成本）", "毛净值"],
      top: 0,
      right: 8,
      textStyle: { color: "#8d9aab", fontSize: 10 },
      itemWidth: 16,
      itemHeight: 8,
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(8,13,22,.94)",
      borderColor: "#263449",
      textStyle: { color: "#e2e8f0", fontSize: 11 },
      valueFormatter: (value) => Number(value).toFixed(4),
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: curve.map((point) => point.label),
      axisLine: { lineStyle: { color: "#263449" } },
      axisTick: { show: false },
      axisLabel: { color: "#718096", fontSize: 9, interval: Math.max(0, Math.ceil(curve.length / 10) - 1) },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: "#182334" } },
      axisLabel: { color: "#718096", fontSize: 10, formatter: (v) => v.toFixed(2) },
    },
    series: [
      {
        name: "净值（扣成本）",
        type: "line",
        smooth: false,
        symbol: "circle",
        symbolSize: 5,
        data: curve.map((point) => point.equity),
        lineStyle: { color: "#3ec6ff", width: 2 },
        itemStyle: { color: "#3ec6ff" },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(62,198,255,.22)" },
              { offset: 1, color: "rgba(62,198,255,0)" },
            ],
          },
        },
        markLine: {
          silent: true,
          symbol: "none",
          data: [{ yAxis: 1 }],
          lineStyle: { color: "rgba(148,163,184,.45)", type: "dashed", width: 1 },
          label: { show: false },
        },
        markArea,
      },
      {
        name: "毛净值",
        type: "line",
        smooth: false,
        symbol: "none",
        data: curve.map((point) => point.gross_equity),
        lineStyle: { color: "#a78bfa", width: 1.4, type: "dashed" },
        itemStyle: { color: "#a78bfa" },
      },
    ],
  }, true);
  equityChart.resize();
}

/* ---------- 逐期 / 跳过 ---------- */

function renderPeriods(payload) {
  const rows = payload.periods || [];
  document.querySelector("#period-rows").innerHTML = rows.length
    ? rows.map((period) => `<tr>
        <td class="mono">${escapeHtml(period.as_of)}</td>
        <td class="mono">${formatNumber(period.n_holdings, 0)}</td>
        <td class="mono ${signOf(period.gross_return)}">${formatPercent(period.gross_return)}</td>
        <td class="mono ${signOf(period.net_return)}">${formatPercent(period.net_return)}</td>
        <td class="mono">${formatPercent(period.turnover, 0)}</td>
        <td class="mono">${formatNumber(period.equity, 4)}</td>
      </tr>`).join("")
    : `<tr><td colspan="6"><div class="empty-state">没有已结算的调仓期</div></td></tr>`;

  const SKIP_TEXT = {
    return_not_backfilled: "收益未回填",
    no_picks_on_day: "当天无选股",
  };
  const skipped = payload.skipped || [];
  document.querySelector("#skip-hint").textContent = skipped.length ? `共 ${skipped.length} 期` : "";
  document.querySelector("#skip-rows").innerHTML = skipped.length
    ? skipped.map((item) => `<div class="kv">
        <span>${escapeHtml(item.as_of)} · ${escapeHtml(SKIP_TEXT[item.reason] || item.reason)}</span>
        <span>${item.n_missing ? `缺 ${formatNumber(item.n_missing, 0)} 只` : "—"}</span>
      </div>`).join("")
    : `<p class="muted" style="margin:0;font-size:11px">没有期次被跳过</p>`;
}

function renderAssumptions(payload) {
  const a = payload.assumptions || {};
  document.querySelector("#assumptions").innerHTML = [
    kv("调仓口径", a.mode_note || a.mode || "—"),
    kv("双边成本", a.cost_bps == null ? "—" : `${formatNumber(a.cost_bps, 1)} bp`),
    kv("成本计法", a.cost_note || "—"),
    kv("权重", a.weighting || "—"),
  ].join("");

  const c = payload.coverage || {};
  document.querySelector("#coverage").innerHTML = [
    kv("台账截面", formatNumber(c.available_days, 0)),
    kv("应有期次", formatNumber(c.scheduled_periods, 0)),
    kv("已测期次", formatNumber(c.measured_periods, 0)),
    kv("跳过期次", formatNumber(c.skipped_periods, 0)),
    kv("中间有洞", c.has_interior_gap ? "是" : "否"),
  ].join("");
}

const kv = (label, value) => `<div class="kv"><span>${escapeHtml(label)}</span><span>${escapeHtml(String(value))}</span></div>`;

/* ---------- 多策略并排 ---------- */

function renderCompare(payload) {
  const items = payload.items || [];
  const usable = items.filter((item) => item.available);
  document.querySelector("#compare-meta").innerHTML = `${escapeHtml(HORIZON_LABEL[payload.horizon] || payload.horizon)} · 成本 ${formatNumber(payload.cost_bps, 1)}bp<br>${usable.length}/${items.length} 个策略可测`;

  document.querySelector("#compare-rows").innerHTML = items.length
    ? items.map((item) => compareRow(item)).join("")
    : `<tr><td colspan="10"><div class="empty-state">台账里还没有任何策略</div></td></tr>`;

  renderCompareChart(usable);
}

function compareRow(item) {
  const m = item.metrics || {};
  const c = item.coverage || {};
  if (!item.available) {
    const [title] = reasonOf(item.missing_reason);
    return `<tr>
      <td>${escapeHtml(item.strategy)}</td>
      <td colspan="8" class="warning">${escapeHtml(title)}</td>
      <td class="mono">${formatNumber(c.measured_periods, 0)}/${formatNumber(c.scheduled_periods, 0)}</td>
    </tr>`;
  }
  const cell = (value, fmt, tone = false) => {
    const empty = value === null || value === undefined;
    return `<td class="mono ${empty ? "muted" : tone ? signOf(value) : ""}">${escapeHtml(empty ? "算不出" : fmt(value))}</td>`;
  };
  return `<tr>
    <td>${escapeHtml(item.strategy)}</td>
    <td class="mono">${formatNumber(m.n_periods, 0)}</td>
    ${cell(m.total_return, formatPercent, true)}
    ${cell(m.gross_total_return, formatPercent, true)}
    ${cell(m.cagr, formatPercent, true)}
    ${cell(m.max_drawdown, formatPercent)}
    ${cell(m.sharpe, (v) => formatNumber(v, 2), true)}
    ${cell(m.win_rate, formatPercent)}
    ${cell(m.avg_turnover, formatPercent)}
    <td class="mono">${formatNumber(c.measured_periods, 0)}/${formatNumber(c.scheduled_periods, 0)}</td>
  </tr>`;
}

// 并排图的横轴是"第几期"而不是日期:各策略的调仓日可能不同,
// 强行按日期对齐会把不同天的净值画在同一个刻度上。日期放进 tooltip。
function renderCompareChart(items) {
  const host = document.querySelector("#compare-chart");
  if (!items.length) {
    if (compareChart) { compareChart.dispose(); compareChart = null; }
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">没有可测的策略，不画对比曲线</div>`;
    return;
  }
  if (typeof echarts === "undefined") {
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">图表库未加载</div>`;
    return;
  }
  if (!compareChart) compareChart = echarts.init(host);

  const longest = Math.max(...items.map((item) => (item.equity_curve || []).length));
  const axis = Array.from({ length: longest }, (_, i) => (i === 0 ? "起点" : `第${i}期`));
  const palette = ["#3ec6ff", "#a78bfa", "#3da678", "#c49a4a", "#c85d67", "#5eead4"];
  const labels = new Map(items.map((item) => [item.strategy, (item.equity_curve || []).map((p) => p.label)]));

  compareChart.setOption({
    backgroundColor: "transparent",
    grid: { top: 26, right: 18, bottom: 30, left: 52 },
    legend: { top: 0, right: 8, textStyle: { color: "#8d9aab", fontSize: 10 }, itemWidth: 16, itemHeight: 8 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(8,13,22,.94)",
      borderColor: "#263449",
      textStyle: { color: "#e2e8f0", fontSize: 11 },
      formatter: (rows) => {
        const head = rows[0]?.axisValue ?? "";
        const body = rows.map((row) => {
          const day = labels.get(row.seriesName)?.[row.dataIndex];
          const suffix = day && day !== head ? ` <span style="color:#8d9aab">${day}</span>` : "";
          return `${row.marker}${row.seriesName} ${Number(row.value).toFixed(4)}${suffix}`;
        });
        return [head, ...body].join("<br>");
      },
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: axis,
      axisLine: { lineStyle: { color: "#263449" } },
      axisTick: { show: false },
      axisLabel: { color: "#718096", fontSize: 9, interval: Math.max(0, Math.ceil(longest / 10) - 1) },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: "#182334" } },
      axisLabel: { color: "#718096", fontSize: 10, formatter: (v) => v.toFixed(2) },
    },
    series: items.map((item, index) => ({
      name: item.strategy,
      type: "line",
      symbol: "none",
      // 期数少于最长的策略在末尾留空,不补最后一个值——补了就等于凭空延长了持仓期
      data: (item.equity_curve || []).map((point) => point.equity),
      lineStyle: { color: palette[index % palette.length], width: 1.8 },
      itemStyle: { color: palette[index % palette.length] },
    })),
  }, true);
  compareChart.resize();
}

/* ---------- 交互 ---------- */

document.querySelector("#refresh")?.addEventListener("click", load);
["#strategy", "#horizon"].forEach((selector) => {
  document.querySelector(selector)?.addEventListener("change", load);
});
["#top-k", "#cost-bps"].forEach((selector) => {
  // change 而不是 input:每敲一个数字就重算会打出一串请求
  document.querySelector(selector)?.addEventListener("change", load);
});
window.addEventListener("resize", () => {
  equityChart?.resize();
  compareChart?.resize();
});
load();
