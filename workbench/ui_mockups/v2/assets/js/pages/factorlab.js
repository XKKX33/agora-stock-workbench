// p4_factorlab.js · 因子实验室页控制器
// 因子覆盖 / 单股贡献来自 /api/factors;机器学习一段全部来自模型产物自带的
// 样本外指标(app/services/analytics.py 转出),页面自己不做任何推算。
//
// 一条硬规则:availability != "available" 时绝不展示任何预测值。
// 但**诊断信息照样展示**——用户要看清"差在哪"才知道下一步做什么,
// 只显示一句"模型不可用"等于什么都没说。
import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("factorlab");

let icChart = null;

async function load() {
  clearError(); setLoading(true);
  try {
    const [data, stocks] = await Promise.all([request("/api/factors"), query("/api/stocks", { selected: true, per_page: 100 })]);
    renderCoverage(data.factors || []);
    const select = document.querySelector("#stock-select");
    select.innerHTML = (stocks.items || []).map((item) => `<option value="${escapeHtml(item.ts_code)}">${escapeHtml(item.name)} · ${escapeHtml(item.ts_code)}</option>`).join("");
    renderMachineLearning(data.machine_learning || {});
    if (select.value) await loadDetail(select.value);
    setStatus(`因子截面 ${data.as_of}`, "ready");
  } catch (error) { showError(error); } finally { setLoading(false); }
}

function renderCoverage(factors) {
  document.querySelector("#factor-coverage").innerHTML = factors.map((item) => `
    <tr><td>${escapeHtml(item.name)}</td><td class="mono">${formatPercent(item.coverage)}</td><td class="mono">${formatNumber(item.average_contribution, 5)}</td></tr>`).join("") || `<tr><td colspan="3"><div class="empty-state">暂无因子数据</div></td></tr>`;
}

async function loadDetail(code) {
  const data = await request(`/api/factors/${encodeURIComponent(code)}`);
  const rows = Object.entries(data.factors || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const max = Math.max(...rows.map(([, value]) => Math.abs(value)), 0.001);
  document.querySelector("#factor-detail").innerHTML = rows.map(([name, value]) => `
    <div class="bar-row"><span>${escapeHtml(name)}</span><span class="bar-track"><span class="bar-fill${value < 0 ? " negative" : ""}" style="width:${Math.abs(value) / max * 100}%"></span></span><span class="mono">${formatNumber(value, 5)}</span></div>`).join("") || `<div class="empty-state">暂无单股因子贡献</div>`;
}

document.querySelector("#stock-select")?.addEventListener("change", (event) => loadDetail(event.target.value).catch(showError));
document.querySelector("#refresh")?.addEventListener("click", load);
load();

/* ---------- 机器学习复核 ---------- */

function renderMachineLearning(ml) {
  const stateEl = document.querySelector("#ml-state");
  const reasonEl = document.querySelector("#ml-reason");
  if (!stateEl || !reasonEl) return;

  const TAGS = {
    available: ["可用", "good"],
    pending: ["未达门槛", "pending"],
    not_trained: ["未训练", "muted"],
  };
  const [label, kind] = TAGS[ml.availability] || TAGS.not_trained;
  stateEl.innerHTML = statusTag(label, kind);
  reasonEl.textContent = ml.reason || "暂无模型状态说明";

  renderGate(ml);
  renderDiagnostics(ml);
}

// 门槛清查:逐项显示实际值 / 要求值。三种状态——达标、不达标、算不出。
// "算不出"单独一档:样本不足导致 IC 为 None,和"算出来是 0"不是一回事。
function renderGate(ml) {
  const host = document.querySelector("#ml-gate");
  if (!host) return;
  const th = ml.thresholds || {};
  const metrics = ml.metrics || {};
  if (!Object.keys(th).length) { host.innerHTML = ""; return; }

  const items = [
    gateItem("样本外截面数", metrics.n_days, th.min_train_days, (v) => formatNumber(v, 0)),
    gateItem("样本外样本数", metrics.n_samples, th.min_samples, (v) => formatNumber(v, 0)),
    gateItem("样本外 IC 均值", metrics.ic_mean, th.min_ic, (v) => formatNumber(v, 4)),
  ];
  host.innerHTML = items.join("");
}

function gateItem(name, actual, required, fmt) {
  const hasValue = actual !== null && actual !== undefined && !Number.isNaN(Number(actual));
  // 没有产物时 metrics 是空对象,此时显示"算不出"而不是把缺失当成 0 判不达标
  const kind = !hasValue ? "unknown" : Number(actual) >= Number(required) ? "pass" : "fail";
  const mark = { pass: "✓", fail: "✕", unknown: "?" }[kind];
  const shown = hasValue ? fmt(actual) : "算不出";
  return `<div class="gate-item ${kind}">
    <span class="gate-mark" aria-hidden="true">${mark}</span>
    <span class="gate-text">${escapeHtml(name)} <span class="muted">≥ ${escapeHtml(fmt(required))}</span></span>
    <span class="gate-value">${escapeHtml(shown)}</span>
  </div>`;
}

function renderDiagnostics(ml) {
  const panel = document.querySelector("#ml-diagnostics");
  if (!panel) return;
  const diag = ml.diagnostics;
  // 没有产物 -> 整段隐藏。空面板比没有面板更让人困惑。
  if (!diag) { panel.hidden = true; return; }
  panel.hidden = false;

  const metrics = ml.metrics || {};
  const params = diag.params || {};
  document.querySelector("#ml-meta").innerHTML = [
    `${escapeHtml(diag.trained_at || "训练时间未知")}`,
    `${escapeHtml(ml.backend || "?")} · ${escapeHtml(diag.horizon || "?")} · ${escapeHtml(String(params.n_splits ?? "?"))} 折`,
  ].join("<br>");

  renderMetricCards(metrics, diag);
  renderIcChart(diag.daily_ic || []);
  renderBuckets(metrics.buckets || diag.buckets || [], metrics.monotonic ?? diag.monotonic);
  renderFolds(diag.folds || []);
  renderProvenance(diag);
}

// 四张卡:样本外 IC / IC_IR / AUC / 过拟合缺口。
// 过拟合缺口 = 训练 IC − 样本外 IC,是"这个模型能不能信"最直接的一眼判断。
function renderMetricCards(metrics, diag) {
  const host = document.querySelector("#ml-metrics");
  if (!host) return;
  const gap = diag.overfit_gap ?? metrics.overfit_gap;
  const cards = [
    metricCard("样本外 IC", metrics.ic_mean, 4, "排序相关性,越高越好", signOf(metrics.ic_mean)),
    metricCard("IC 信息比", metrics.ic_ir, 3, "IC 均值 / 波动,稳定性", signOf(metrics.ic_ir)),
    metricCard("AUC", metrics.auc, 4, "0.5 = 无区分度", metrics.auc == null ? "" : Number(metrics.auc) > 0.5 ? "positive" : "negative"),
    metricCard("过拟合缺口", gap, 4, "训练 IC − 样本外 IC", gap == null ? "" : Number(gap) > 0.1 ? "negative" : "positive"),
  ];
  host.innerHTML = cards.join("");
}

function metricCard(label, value, digits, note, tone) {
  const shown = value === null || value === undefined ? "算不出" : formatNumber(value, digits);
  const cls = value === null || value === undefined ? "muted" : tone;
  return `<article class="panel metric">
    <div class="metric-label">${escapeHtml(label)}</div>
    <div class="metric-value ${cls}">${escapeHtml(shown)}</div>
    <div class="metric-note">${escapeHtml(note)}</div>
  </article>`;
}

function signOf(value) {
  if (value === null || value === undefined) return "";
  return Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "muted";
}

// 逐日样本外 IC。柱状,零轴居中——正负各半的形态一眼就能看出"模型在反着排"。
function renderIcChart(daily) {
  const host = document.querySelector("#ml-ic-chart");
  const hint = document.querySelector("#ml-ic-hint");
  if (!host) return;
  if (!daily.length) {
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">无逐日 IC 数据</div>`;
    if (hint) hint.textContent = "";
    return;
  }
  const negative = daily.filter((d) => Number(d.ic) < 0).length;
  if (hint) hint.textContent = `共 ${daily.length} 个截面,其中 ${negative} 个为负`;

  if (typeof echarts === "undefined") {
    host.innerHTML = `<div class="empty-state" style="min-height:0;height:100%;border:0">图表库未加载</div>`;
    return;
  }
  if (!icChart) icChart = echarts.init(host);
  icChart.setOption({
    backgroundColor: "transparent",
    grid: { top: 18, right: 14, bottom: 26, left: 48 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(8,13,22,.94)",
      borderColor: "#263449",
      textStyle: { color: "#e2e8f0", fontSize: 11 },
      formatter: (items) => {
        const it = items[0];
        return `${it.axisValue}<br>IC ${Number(it.data).toFixed(4)}`;
      },
    },
    xAxis: {
      type: "category",
      data: daily.map((d) => String(d.as_of ?? d.day ?? "")),
      axisLine: { lineStyle: { color: "#263449" } },
      axisTick: { show: false },
      axisLabel: { color: "#718096", fontSize: 9, interval: Math.max(0, Math.ceil(daily.length / 8) - 1) },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#182334" } },
      axisLabel: { color: "#718096", fontSize: 10, formatter: (v) => v.toFixed(2) },
    },
    series: [{
      type: "bar",
      data: daily.map((d) => Number(d.ic)),
      barMaxWidth: 18,
      itemStyle: {
        // 正负分色:用主题的 positive/negative,不用 A 股涨红跌绿——
        // 这里是"模型对不对",不是"股票涨没涨"
        color: (p) => (p.data >= 0 ? "#3da678" : "#c85d67"),
      },
    }],
  }, true);
  icChart.resize();
}

// 分桶收益。理想形态是桶 1 最高、逐桶递减;递增说明模型排序方向反了。
function renderBuckets(buckets, monotonic) {
  const host = document.querySelector("#ml-buckets");
  if (!host) return;
  if (!buckets.length) {
    host.innerHTML = `<div class="empty-state">无分桶数据</div>`;
    return;
  }
  // 字段名对齐 engine/ml/metrics.decile_returns:{bucket, n, avg_return}
  const max = Math.max(...buckets.map((b) => Math.abs(Number(b.avg_return) || 0)), 1e-6);
  const rows = buckets.map((b) => {
    const empty = b.avg_return === null || b.avg_return === undefined;
    const value = Number(b.avg_return);
    const width = empty ? 0 : Math.abs(value) / max * 100;
    return `<div class="bar-row">
      <span>桶 ${escapeHtml(String(b.bucket))}<span class="muted"> ·${escapeHtml(formatNumber(b.n, 0))}</span></span>
      <span class="bar-track"><span class="bar-fill${!empty && value < 0 ? " negative" : ""}" style="width:${width}%"></span></span>
      <span class="mono">${escapeHtml(empty ? "算不出" : formatNumber(value, 4))}</span>
    </div>`;
  });
  const verdict = monotonic === true
    ? `<p class="muted" style="margin:12px 0 0;font-size:11px">分桶单调递减,排序方向正确</p>`
    : monotonic === false
      ? `<p class="negative" style="margin:12px 0 0;font-size:11px">分桶非单调递减——排序未能区分强弱,甚至可能方向相反</p>`
      : "";
  host.innerHTML = rows.join("") + verdict;
}

// 分折明细。训练区间 / 隔离带 / 测试区间三列并排,是 purge 有没有真正生效的凭证。
// 字段名对齐 engine/ml/splits.Fold.as_dict:给的是区间端点 + 天数,不是日期数组。
// Fold.index 是 0 起的下标,显示时 +1 —— 表头写"折"却从 0 开始数会让人误读。
function renderFolds(folds) {
  const host = document.querySelector("#ml-folds");
  if (!host) return;
  if (!folds.length) {
    host.innerHTML = `<tr><td colspan="8"><div class="empty-state">无分折数据</div></td></tr>`;
    return;
  }
  host.innerHTML = folds.map((fold, index) => {
    const m = fold.metrics || {};
    return `<tr>
      <td class="mono">${escapeHtml(String((fold.index ?? index) + 1))}</td>
      <td class="mono">${range(fold.train_start, fold.train_end, fold.n_train_days)}</td>
      <td class="mono">${escapeHtml(formatNumber(fold.n_purged_days, 0))} 日</td>
      <td class="mono">${range(fold.test_start, fold.test_end, fold.n_test_days)}</td>
      <td class="mono">${escapeHtml(formatNumber(m.n_samples, 0))}</td>
      <td class="mono ${signOf(m.ic_mean)}">${escapeHtml(m.ic_mean == null ? "—" : formatNumber(m.ic_mean, 4))}</td>
      <td class="mono">${escapeHtml(m.auc == null ? "—" : formatNumber(m.auc, 4))}</td>
      <td class="mono">${escapeHtml(m.hit_rate == null ? "—" : formatPercent(m.hit_rate))}</td>
    </tr>`;
  }).join("");
}

function range(start, end, count) {
  if (!start && !end) return "—";
  const suffix = count == null ? "" : ` <span class="muted">(${escapeHtml(String(count))})</span>`;
  return `${escapeHtml(String(start ?? "?"))} → ${escapeHtml(String(end ?? "?"))}${suffix}`;
}

// 数据出处。模型是在什么口径、多少截面、多少候选上训出来的,
// 不写在页面上就只能翻命令行历史。
// 字段名对齐 engine/ml/dataset.DatasetReport.as_dict。
function renderProvenance(diag) {
  const host = document.querySelector("#ml-provenance");
  if (!host) return;
  const ds = diag.dataset || {};
  const params = diag.params || {};
  const parts = [];
  if (ds.replayed_days != null) parts.push(`回放 ${ds.replayed_days}/${ds.requested_days ?? "?"} 个截面`);
  if (ds.n_rows != null) parts.push(`样本 ${ds.n_rows} 行`);
  if (ds.n_features != null) parts.push(`特征 ${ds.n_features} 个`);
  // 标签截止日:这一天之后的截面因为 T+N 还没发生而没被采样,不是数据缺失
  if (ds.label_cutoff) parts.push(`标签截止 ${ds.label_cutoff}`);
  if (params.stride != null) parts.push(`步长 ${params.stride}`);
  if (params.candidate_limit != null) parts.push(`候选上限 ${params.candidate_limit}`);
  if (params.embargo_days != null) parts.push(`隔离带 ${params.embargo_days} 日`);
  if (params.top_k != null) parts.push(`top 桶 ${params.top_k}`);

  const skipped = Object.entries(ds.skipped_days || {});
  // labels.needs_attention 已经剔掉了 future_not_reached(等未来,正常),
  // 剩下的都是真的要人处理的缺失。字段名对齐 engine/ml/labels.LabelReport。
  const attention = Object.entries((ds.labels && ds.labels.needs_attention) || {});
  const tail = [];
  if (ds.labels && ds.labels.resolved != null) tail.push(`标签可用 ${ds.labels.resolved}`);
  if (skipped.length) tail.push(`跳过截面 ${skipped.map(([k, v]) => `${k}×${v}`).join(" ")}`);
  if (attention.length) tail.push(`标签异常 ${attention.map(([k, v]) => `${k}×${v}`).join(" ")}`);

  host.textContent = [parts.join(" · "), tail.join(" · ")].filter(Boolean).join("  |  ");
}

window.addEventListener("resize", () => icChart?.resize());
