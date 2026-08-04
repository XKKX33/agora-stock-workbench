import { request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, statusTag } from "/assets/js/format.js";

initShell("ai");

// 三类标注的中文名与配色，与后端 LABEL_LEGEND 一一对应
const LABEL_TAG = {
  fact: ["事实", "good"],
  derived: ["规则计算", "active"],
  unverified: ["待验证", "pending"],
};

// 通用渲染时把常见英文键翻成中文；没收录的键原样显示，不编造含义
const KEY_LABELS = {
  trade_date: "交易日", strategy: "策略", run_id: "批次", generated_at: "生成时间",
  total_symbols: "股票总数", quoted_symbols: "有涨跌幅家数", missing_pct_chg: "缺涨跌幅家数",
  up: "上涨家数", down: "下跌家数", flat: "平盘家数", near_limit_up: "近涨停家数",
  near_limit_up_threshold: "近涨停阈值", avg_pct_chg: "平均涨跌幅", median_pct_chg: "中位涨跌幅",
  total_amount: "总成交额", industry: "行业", count: "家数", avg_pct: "平均涨跌幅",
  med_pct: "中位涨跌幅", up_ratio: "上涨占比", strong_ratio: "强势占比", heat: "热度",
  scored: "已打分", passed: "通过门槛", selected: "入选", passed_not_selected: "过门槛未入选",
  rejected: "淘汰", selected_list: "入选名单", reject_reasons: "淘汰原因",
  reject_reason_kinds: "淘汰原因种类", reason: "原因", n_selected: "入选数",
  by_factor: "因子贡献", by_category: "类别贡献", key: "因子", avg: "均值", n: "样本数",
  money_class: "资金确认", unclassified: "缺资金流未分类", by_class: "分布",
  coverage: "覆盖", items: "条目", stocks: "个股对应", industries: "行业对应",
  stocks_with_news: "有舆情个股数", stocks_examined: "已检视个股数", ts_code: "代码",
  name: "名称", pct_chg: "涨跌幅", news: "关联舆情", news_missing_reason: "舆情缺失原因",
  match_basis: "匹配依据", match_text: "匹配文本", confidence: "置信度", source: "来源",
  judgement: "判断", title: "标题", summary: "摘要", url: "链接", published_at: "发布时间",
  fetched_at: "抓取时间", sentiment: "情绪", sentiment_score: "情绪分", credibility: "可信度",
  event_type: "事件类型", duplicate_of: "重复条目", backfill: "收益回填", pending: "待回填",
  pending_reasons: "待回填原因", stats: "样本统计", mode: "模式", horizon: "持有期",
  n_samples: "样本数", n_days: "有效交易日", ic_mean: "IC 均值",
  rank_ic_mean: "秩 IC 均值", ic_ir: "IC IR",
  win_rate: "胜率", profit_factor: "盈亏比", avg_ret: "平均收益", median_ret: "中位收益",
  layer_avg: "分层均值", filled: "已回填", needs_attention: "待处理", total_filled: "累计回填",
};

function labelTag(label) {
  const [text, kind] = LABEL_TAG[label] || [String(label || "未标注"), "muted"];
  return statusTag(text, kind);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ---------------------------------------------------------------- AI 状态
async function loadStatus() {
  try {
    const data = await request("/api/ai/status");
    renderStatus(data);
  } catch (error) {
    showError(error);
    document.querySelector("#ai-state-body").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderStatus(data) {
  const map = { available: ["可用", "good"], disabled: ["未启用", "muted"], unconfigured: ["未配置", "pending"] };
  const [text, kind] = map[data.availability] || [String(data.availability || "未知"), "muted"];
  document.querySelector("#ai-state-tag").innerHTML = statusTag(text, kind);

  let html = `
    <div class="kv"><span>提供方</span><span>${escapeHtml(data.provider || "—")}</span></div>
    <div class="kv"><span>模型</span><span>${escapeHtml(data.model || "—")}</span></div>
    <div class="kv"><span>凭据环境变量</span><span>${escapeHtml(data.api_key_env || "—")}</span></div>
    <div class="kv"><span>状态说明</span><span>${escapeHtml(data.reason || "配置完整，可调用")}</span></div>`;
  if ((data.missing || []).length) {
    html += `<div class="kv-block"><span class="kv-key">还缺什么</span><div class="chip-row">${data.missing.map((item) => statusTag(item, "pending")).join("")}</div></div>`;
  }
  if (data.availability !== "available") {
    html += guideCard(data);
  }
  document.querySelector("#ai-state-body").innerHTML = html;
}

// 未配置 / 未启用时给出真实配置指引，并写明 AI 边界
function guideCard(data) {
  const env = escapeHtml(data.api_key_env || "WORKBENCH_AI_API_KEY");
  return `
    <div class="guide-card">
      <strong>如何启用</strong>
      <p>AI 边界：只基于数据库已入库事实生成复盘；未配置时不会生成任何编造内容。</p>
      <ol>
        <li>settings.yaml 的 <code>ai</code> 段：<code>enabled: true</code></li>
        <li><code>provider</code>：已注册的提供方标识</li>
        <li><code>model</code>：要用的模型名</li>
        <li><code>api_key_env</code>：默认 <code>WORKBENCH_AI_API_KEY</code>；本页读到的是 <code>${env}</code>，把对应环境变量设置好</li>
      </ol>
    </div>`;
}

// ---------------------------------------------------------------- 盘后复盘
async function loadReview() {
  clearError();
  setLoading(true);
  try {
    const data = await request("/api/reviews");
    renderLegend(data.label_legend);
    renderMeta(data);
    renderSections(data.sections || {});
    setStatus(`复盘 ${formatDate(data.trade_date)}`, "ready");
  } catch (error) {
    showError(error);
    document.querySelector("#review-sections").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  } finally {
    setLoading(false);
  }
}

function renderLegend(legend) {
  const bar = document.querySelector("#legend-bar");
  if (!legend || !Object.keys(legend).length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.innerHTML = Object.entries(legend)
    .map(([key, desc]) => `<span class="legend-item">${labelTag(key)}<span>${escapeHtml(desc)}</span></span>`)
    .join("");
}

function renderMeta(data) {
  document.querySelector("#review-title").textContent = `盘后复盘 · ${formatDate(data.trade_date)}`;
  document.querySelector("#review-meta").textContent =
    `${data.strategy ? `策略 ${data.strategy}` : "策略 未限定"} · ${data.run_id ? `批次 ${data.run_id}` : "无扫描批次"} · 生成于 ${formatTime(data.generated_at)}`;
}

function renderSections(sections) {
  const box = document.querySelector("#review-sections");
  const entries = Object.entries(sections);
  if (!entries.length) {
    box.innerHTML = `<div class="empty-state">暂无复盘数据</div>`;
    return;
  }
  box.innerHTML = entries.map(([key, section]) => `
    <article class="panel section-panel">
      <div class="panel-header">
        <div><h2>${escapeHtml(section.title || key)}</h2><p class="mono">${escapeHtml(key)}</p></div>
        <div>${labelTag(section.label)}</div>
      </div>
      ${section.available ? renderSectionData(section.data) : renderMissing(section)}
      ${section.note ? `<p class="section-note">注：${escapeHtml(section.note)}</p>` : ""}
    </article>`).join("");
}

function renderSectionData(data) {
  if (data === null || data === undefined) return `<div class="empty-state">暂无数据</div>`;
  if (typeof data === "string" || typeof data === "number" || typeof data === "boolean") {
    return `<div class="section-value">${renderValue(data)}</div>`;
  }
  return renderValue(data);
}

function renderMissing(section) {
  return `
    <div class="empty-state">
      <div class="missing-stack">
        <span class="tag pending">${escapeHtml(section.missing_reason || "missing")}</span>
        <span class="detail">${escapeHtml(section.detail || "暂无数据")}</span>
      </div>
    </div>`;
}

// 通用渲染：dict 转 kv 行，list 转表格，字符串直接显示
function renderValue(value) {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return renderTable(value);
  if (typeof value === "object") return renderKv(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return escapeHtml(String(value));
}

function renderKv(obj) {
  return Object.entries(obj).map(([key, value]) => {
    if (value !== null && typeof value === "object") {
      return `<div class="kv-block"><span class="kv-key">${escapeHtml(keyLabel(key))}</span><div>${renderValue(value)}</div></div>`;
    }
    return `<div class="kv"><span>${escapeHtml(keyLabel(key))}</span><span>${renderValue(value)}</span></div>`;
  }).join("");
}

function renderTable(rows) {
  if (!rows.length) return `<div class="empty-state">暂无明细</div>`;
  const headers = [];
  rows.forEach((row) => Object.keys(row || {}).forEach((key) => {
    if (!headers.includes(key)) headers.push(key);
  }));
  const body = rows.map((row) => `<tr>${headers.map((header) => `<td>${renderCell(row?.[header])}</td>`).join("")}</tr>`).join("");
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((header) => `<th>${escapeHtml(keyLabel(header))}</th>`).join("")}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function renderCell(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return `<span class="mono muted">${escapeHtml(JSON.stringify(value))}</span>`;
  return escapeHtml(String(value));
}

function keyLabel(key) {
  return KEY_LABELS[key] || key;
}

// ---------------------------------------------------------------- 生成 AI 复盘
async function generateReview() {
  clearError();
  const btn = document.querySelector("#generate-btn");
  const stateEl = document.querySelector("#generate-state");
  btn.disabled = true;
  btn.textContent = "生成中…";
  stateEl.textContent = "正在请求 AI…";
  try {
    // 未配置时接口返回 503 + ai_unavailable，showError 展示真实原因，不编造
    const data = await request("/api/ai/reviews", { method: "POST" });
    renderNarrative(data);
    setStatus("AI 复盘已生成", "ready");
  } catch (error) {
    showError(error);
    setStatus("AI 生成失败", "error");
    loadStatus();
  } finally {
    btn.disabled = false;
    btn.textContent = "生成 AI 复盘";
    stateEl.textContent = "";
  }
}

function renderNarrative(data) {
  const panel = document.querySelector("#narrative-panel");
  panel.hidden = false;
  document.querySelector("#narrative-meta").textContent =
    `${formatDate(data.trade_date)} · ${data.provider || "AI"} ${data.model || ""}`.trim();
  document.querySelector("#narrative-text").textContent = data.narrative || "（返回内容为空）";
  const grounded = data.grounded_in || [];
  const missing = (data.missing || []).map((item) => item.section || item.detail).filter(Boolean);
  const notes = [];
  if (grounded.length) notes.push(`依据小节：${grounded.join("、")}（叙述只能重述这些已入库事实）`);
  if (missing.length) notes.push(`未覆盖小节：${missing.join("、")}`);
  document.querySelector("#narrative-notes").innerHTML = notes
    .map((note) => `<p class="section-note">${escapeHtml(note)}</p>`)
    .join("");
}

document.querySelector("#generate-btn").addEventListener("click", generateReview);

loadStatus();
loadReview();