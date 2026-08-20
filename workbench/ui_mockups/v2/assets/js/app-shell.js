import { refreshDataLinks } from "/assets/js/data-links.js";

const THEME_KEY = "hermes-theme";
const WORK_CONTEXT_KEY = "hermes.work-context";
const WORK_CONTEXT_FIELDS = ["run_id", "strategy", "as_of", "data_cutoff", "availability", "missing_reason"];
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
function renderWorkContext(context = getWorkContext()) {
  const host = document.querySelector("#work-context");
  if (!host) return;
  const availability = context.availability || "available";
  const state = availability === "available" ? "可用" : availability === "partial" ? "部分可用" : "不可用";
  const reason = context.missing_reason ? ` · ${context.missing_reason}` : "";
  const batch = context.run_id ? `批次 ${context.run_id}` : "未选择批次";
  const date = context.as_of ? ` · 信号日 ${context.as_of}` : "";
  const strategy = context.strategy ? ` · ${context.strategy}` : "";
  const cutoff = context.data_cutoff ? ` · 截止 ${context.data_cutoff}` : "";
  host.innerHTML = `<span class="work-context-label">当前工作上下文</span><span class="work-context-value">${batch}${strategy}${date}${cutoff}</span><span class="work-context-state">${state}${reason}</span>`;
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

// 十二个入口：key / 目标页 / 中文名 / 小图标（unicode 字形，不引入字体文件）
const NAV = [
  ["overview", "index.html", "总览", "◉"],
  ["desk", "p1_desk.html", "选股台", "▤"],
  ["sentiment", "p2_sentiment.html", "情绪", "◒"],
  ["foundry", "p3_foundry.html", "流程", "▦"],
  ["factorlab", "p4_factorlab.html", "因子", "◬"],
  ["ledger", "p5_ledger.html", "台账", "≡"],
  ["backtest", "p9_backtest.html", "回测", "◪"],
  ["chart", "p6_chart.html", "行情", "◧"],
  ["watchlist", "p10_watchlist.html", "自选", "★"],
  ["news", "p7_news.html", "舆情", "◈"],
  ["ai", "p8_ai.html", "AI复盘", "✦"],
  ["agents", "p11_agents.html", "AI Agent", "⚡"],
  ["agent-dashboard", "p13_agent_dashboard.html", "研判看板", "▥"],
  ["settings", "p12_settings.html", "设置", "⚙"],
];

export function initShell(page) {
  const host = document.querySelector("#app-shell");
  if (!host) return;
  host.innerHTML = `
    <aside class="sidebar">
      <a class="brand" href="index.html">
        <span class="brand-mark">H</span>
        <span><strong>Hermes Quant</strong><span>本地量化工作台</span></span>
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
  renderWorkContext(getWorkContext());
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
