import { request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, statusTag } from "/assets/js/format.js";
import { aiState, newsState, reviewState } from "/assets/js/data-links.js";

initShell("overview");

async function load() {
  clearError();
  setLoading(true);
  try {
    const data = await request("/api/overview");
    document.querySelector("#trade-date").textContent = formatDate(data.latest_trade_date);
    renderMetrics(data);
    renderPicks(data.latest_scan?.picks || []);
    renderTables(data.tables || {});
    renderLinkStatus();
    document.querySelector("#updated-at").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN")}`;
    setStatus("DuckDB 已连接", "ready");
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

function renderMetrics(data) {
  const scan = data.latest_scan;
  document.querySelector("#metric-candidates").textContent = scan ? formatNumber(scan.candidate_count, 0) : "—";
  document.querySelector("#metric-scored").textContent = scan ? formatNumber(scan.scored_count, 0) : "—";
  document.querySelector("#metric-passed").textContent = scan ? formatNumber(scan.passed_count, 0) : "—";
  document.querySelector("#metric-final").textContent = scan ? formatNumber(scan.final_count, 0) : "—";
  document.querySelector("#scan-state").innerHTML = data.scan_job
    ? statusTag(data.scan_job.status, "active")
    : statusTag("空闲", "good");
}

function renderPicks(picks) {
  const body = document.querySelector("#pick-rows");
  if (!picks.length) {
    body.innerHTML = `<tr><td colspan="6"><div class="empty-state">尚无入选股票<br>前往选股台运行扫描</div></td></tr>`;
    return;
  }
  body.innerHTML = picks.map((item) => `
    <tr data-code="${escapeHtml(item.ts_code)}">
      <td class="mono">${item.rank}</td><td><strong>${escapeHtml(item.name)}</strong><br><span class="muted mono">${escapeHtml(item.ts_code)}</span></td>
      <td>${escapeHtml(item.industry)}</td><td class="mono accent">${formatNumber(item.total, 4)}</td>
      <td>${statusTag(item.money_class || "未确认")}</td><td class="muted">${escapeHtml(item.one_line)}</td>
    </tr>`).join("");
  body.querySelectorAll("tr[data-code]").forEach((row) => row.addEventListener("click", () => {
    location.href = `p1_desk.html?code=${encodeURIComponent(row.dataset.code)}`;
  }));
}

function renderTables(tables) {
  const body = document.querySelector("#table-status");
  body.innerHTML = Object.entries(tables).map(([name, item]) => `
    <div class="kv"><span>${escapeHtml(name)}</span><span>${formatNumber(item.row_count, 0)} · ${formatDate(item.latest_date)}</span></div>`).join("");
}

document.querySelector("#refresh")?.addEventListener("click", load);
load();

async function renderLinkStatus() {
  const container = document.querySelector("#table-status");
  if (!container) return;
  container.querySelectorAll("[data-link-row]").forEach((el) => el.remove());
  const states = await Promise.all([newsState(), reviewState(), aiState()]);
  const names = ["舆情", "复盘", "AI 复盘"];
  container.insertAdjacentHTML("beforeend", states.map((state, index) => `
    <div class="kv" data-link-row title="${escapeHtml(state.detail)}"><span>${names[index]}</span><span class="link-state ${state.kind}">${escapeHtml(state.label)}</span></div>`).join(""));
}
