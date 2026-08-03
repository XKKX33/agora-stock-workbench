import { refreshDataLinks } from "/assets/js/data-links.js";

// 十一个入口：key / 目标页 / 中文名 / 小图标（unicode 字形，不引入字体文件）
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
        <div class="status-line"><span id="status-dot" class="status-dot"></span><span id="app-status">正在连接本地数据</span></div>
      </div>
    </aside>`;
  refreshDataLinks(document.querySelector("#data-link-rows")).catch(() => {});
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