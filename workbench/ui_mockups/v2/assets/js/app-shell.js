import { refreshDataLinks } from "/assets/js/data-links.js";

const THEME_KEY = "hermes-theme";
const WORK_CONTEXT_KEY = "hermes.work-context";
const WORK_CONTEXT_FIELDS = ["run_id", "strategy", "as_of", "data_cutoff", "availability", "missing_reason", "candidate_codes"];
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

function readStoredContext() {
  try {
    const value = JSON.parse(localStorage.getItem(WORK_CONTEXT_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function cleanContext(value) {
  return WORK_CONTEXT_FIELDS.reduce((context, field) => {
    if (value?.[field] !== undefined && value[field] !== null && value[field] !== "") context[field] = value[field];
    return context;
  }, {});
}

export function getWorkContext() {
  return cleanContext(readStoredContext());
}

export function setWorkContext(next = {}) {
  const context = { ...getWorkContext() };
  WORK_CONTEXT_FIELDS.forEach((field) => {
    if (!Object.prototype.hasOwnProperty.call(next, field)) return;
    const value = next[field];
    if (value === undefined || value === null || value === "") delete context[field];
    else context[field] = value;
  });
  localStorage.setItem(WORK_CONTEXT_KEY, JSON.stringify(context));
  window.dispatchEvent(new CustomEvent("hermes:work-context", { detail: context }));
  renderWorkContext(context);
  return context;
}

export function workContextParams(context = getWorkContext()) {
  return cleanContext(context);
}
/** 批次运行时刻。同一信号日可以跑多次，只给日期分不清是哪一次，所以带到分钟。
 *  返回 `月-日 时:分`；调用方已单独显示信号日，这里只表达「什么时候跑的」。 */
function batchStamp(value) {
  if (!value) return "";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return match ? `${match[2]}-${match[3]} ${match[4]}:${match[5]}` : String(value).slice(0, 16);
}

function signalDateText(value) {
  const text = String(value || "");
  return /^\d{8}$/.test(text) ? `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6)}` : text;
}

// 批次列表拉一次缓存起来：侧栏每页都渲染，逐页请求纯属浪费；切换批次不改变列表本身。
let batchCache = null;

async function loadBatchOptions() {
  if (batchCache) return batchCache;
  try {
    const response = await fetch("/api/experiments/batches");
    if (!response.ok) return [];
    const payload = await response.json();
    batchCache = Array.isArray(payload.items) ? payload.items : [];
  } catch {
    // 批次列表拿不到不该让整个侧栏崩掉，退化成只显示当前上下文。
    batchCache = [];
  }
  return batchCache;
}

function renderWorkContext(context = getWorkContext()) {
  const host = document.querySelector("#work-context");
  if (!host) return;
  const availability = context.availability || "available";
  const state = availability === "available" ? "可用" : availability === "partial" ? "部分可用" : "不可用";
  const reason = context.missing_reason ? ` · ${context.missing_reason}` : "";
  const strategy = context.strategy ? ` · ${context.strategy}` : "";
  const cutoff = context.data_cutoff ? ` · 截止 ${context.data_cutoff}` : "";
  const picked = context.run_id || "";
  const options = (batchCache || []).map((item) => {
    const label = `信号 ${signalDateText(item.as_of)} · 运行 ${batchStamp(item.created_at) || "时间未记录"}`;
    const selected = item.run_id === picked ? " selected" : "";
    return `<option value="${item.run_id}"${selected}>${label}</option>`;
  });
  // 上下文里的批次不在列表里（比如刚跑完还没刷新列表）时补一项，否则下拉框会显示成别的批次。
  if (picked && !(batchCache || []).some((item) => item.run_id === picked)) {
    const label = `${signalDateText(context.as_of) || "当前"} · ${String(picked).slice(0, 8)}`;
    options.unshift(`<option value="${picked}" selected>${label}</option>`);
  }
  host.innerHTML = `
    <label class="work-context-label" for="work-context-batch">入选批次</label>
    <select id="work-context-batch" class="work-context-select" aria-label="选择入选批次">
      <option value="">最新一次（不锁定）</option>
      ${options.join("")}
    </select>
    <span class="work-context-value">${strategy || " "}${cutoff}</span>
    <span class="work-context-state">${state}${reason}</span>`;
  const select = host.querySelector("#work-context-batch");
  if (select) {
    select.value = picked;
    select.addEventListener("change", (event) => {
      const runId = event.target.value;
      // 选空 = 解除锁定，回到各页默认的「最新一次」。
      if (!runId) {
        setWorkContext({ run_id: "", as_of: "", strategy: "" });
      } else {
        const item = (batchCache || []).find((entry) => entry.run_id === runId);
        setWorkContext({
          run_id: runId,
          as_of: item?.as_of || "",
          strategy: item?.strategy_name || "",
        });
      }
      // 各页监听 hermes:work-context 自行重载；这里只负责改上下文，不知道页面怎么用它。
      window.dispatchEvent(new CustomEvent("hermes:batch-changed", { detail: { run_id: runId } }));
    });
  }
}

function savedTheme() {
  const value = localStorage.getItem(THEME_KEY);
  return value === "light" || value === "dark" ? value : null;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = document.querySelector("#theme-toggle");
  if (!button) return;
  const dark = theme === "dark";
  button.querySelector("[data-theme-icon]").textContent = dark ? "☼" : "☾";
  button.setAttribute("aria-pressed", String(dark));
}

applyTheme(savedTheme() || (systemTheme.matches ? "dark" : "light"));
systemTheme.addEventListener("change", (event) => {
  if (!savedTheme()) applyTheme(event.matches ? "dark" : "light");
});

// 主流程入口。台账与回测原先不在侧栏，只能靠 p3 里的两个文字链接或手敲地址进——
// 结果是「改了台账页」用户从侧栏根本找不到，误以为改动没生效。凡是有独立结论要看的页面
// 都必须有侧栏入口。
const NAV = [
  ["overview", "index.html", "总览", "◉"],
  ["desk", "p1_desk.html", "方法论选股", "▤"],
  ["news", "p7_news.html", "板块舆情", "◈"],
  ["agents", "p11_agents.html", "多 Agent 辩论", "⚡"],
  ["ledger", "p5_ledger.html", "实验台账", "▦"],
  ["backtest", "p9_backtest.html", "组合回测", "◱"],
  ["watchlist", "p10_watchlist.html", "自选与行情", "★"],
  ["settings", "p12_settings.html", "设置", "⚙"],
];

export function initShell(page) {
  const host = document.querySelector("#app-shell");
  if (!host) return;
  host.innerHTML = `
    <aside class="sidebar">
      <a class="brand" href="index.html">
        <span class="brand-mark">A</span>
        <span><strong>AGORA Quant</strong><span>本地量化工作台</span></span>
      </a>
      <div class="nav-label">Workspace</div>
      <nav class="nav-list">
        ${NAV.map(([key, href, label, icon]) => `<a class="nav-link ${key === page ? "active" : ""}" href="${href}"><span class="nav-ico" aria-hidden="true">${icon}</span>${label}</a>`).join("")}
      </nav>
      <div class="data-links">
        <div class="data-links-title">数据链路</div>
        <div id="data-link-rows">
          <div class="data-link"><span class="data-dot pending"></span><span class="data-link-name">舆情</span><span class="data-link-state">加载中</span></div>
          <div class="data-link"><span class="data-dot pending"></span><span class="data-link-name">复盘</span><span class="data-link-state">加载中</span></div>
          <div class="data-link"><span class="data-dot pending"></span><span class="data-link-name">AI 复盘</span><span class="data-link-state">加载中</span></div>
        </div>
      </div>
      <div class="sidebar-status">
        <button id="theme-toggle" class="theme-toggle" type="button" aria-label="切换明亮与暗夜主题" title="切换明亮与暗夜主题" aria-pressed="false"><span data-theme-icon aria-hidden="true">☾</span></button>
        <div class="status-line"><span id="status-dot" class="status-dot"></span><span id="app-status">正在连接本地数据</span></div>
      </div>
      <div id="work-context" class="work-context" aria-live="polite"></div>
    </aside>`;
  applyTheme(savedTheme() || (systemTheme.matches ? "dark" : "light"));
  document.querySelector("#theme-toggle")?.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
  refreshDataLinks(document.querySelector("#data-link-rows")).catch(() => {});
  // 先用当前上下文渲染一次（下拉框只有「最新一次」），批次列表到了再补全选项。
  // 不等待是刻意的：批次接口慢或挂掉时侧栏仍然立刻可用。
  renderWorkContext(getWorkContext());
  loadBatchOptions().then(() => renderWorkContext(getWorkContext())).catch(() => {});
}

export function setStatus(message, state = "ready") {
  const label = document.querySelector("#app-status");
  const dot = document.querySelector("#status-dot");
  if (label) label.textContent = message;
  if (dot) dot.className = `status-dot ${state}`;
}

export function showError(error) {
  const banner = document.querySelector("#error-banner");
  if (banner) {
    banner.textContent = error?.message || String(error);
    banner.classList.add("visible");
  }
  setStatus("数据请求失败", "error");
}

export function clearError() {
  const banner = document.querySelector("#error-banner");
  if (banner) {
    banner.textContent = "";
    banner.classList.remove("visible");
  }
}

export function setLoading(active) {
  document.querySelector("main")?.classList.toggle("loading", active);
}
