import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatNumber, statusTag } from "/assets/js/format.js";

initShell("foundry");

async function load() {
  clearError(); setLoading(true);
  try {
    const [overview, stocks] = await Promise.all([request("/api/overview"), query("/api/stocks", { per_page: 200, sort: "rank" })]);
    const scan = overview.latest_scan;
    if (!scan) {
      document.querySelectorAll("[data-funnel]").forEach((node) => { node.textContent = "—"; });
      document.querySelector("#selected-rows").innerHTML = emptyRow("暂无扫描批次，请先运行扫描");
      document.querySelector("#rejected-rows").innerHTML = emptyRow("暂无扫描批次");
      setStatus("暂无扫描批次", "ready");
      return;
    }
    const values = [scan.candidate_count, scan.scored_count, scan.passed_count, scan.final_count];
    document.querySelectorAll("[data-funnel]").forEach((node, index) => { node.textContent = formatNumber(values[index], 0); });
    document.querySelector("#selected-rows").innerHTML = stocks.items.filter((item) => item.selected).map(rowHtml).join("") || emptyRow("暂无入选股票");
    document.querySelector("#rejected-rows").innerHTML = stocks.items.filter((item) => !item.passed).slice(0, 20).map((item) => `
      <tr><td><strong>${escapeHtml(item.name)}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td><td>${escapeHtml(item.industry)}</td><td>${statusTag("淘汰", "bad")}</td><td class="muted">${escapeHtml((item.gate_reasons || []).join("；") || "门槛未通过")}</td></tr>`).join("") || emptyRow("没有被淘汰的候选");
    setStatus(`流程批次 ${scan.run_id.slice(0, 8)}`, "ready");
  } catch (error) { showError(error); } finally { setLoading(false); }
}

function rowHtml(item) { return `<tr><td class="mono">${item.rank}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.industry)}</td><td class="mono accent">${formatNumber(item.total, 4)}</td></tr>`; }
function emptyRow(message) { return `<tr><td colspan="4"><div class="empty-state">${message}</div></td></tr>`; }

document.querySelector("#refresh")?.addEventListener("click", load);
load();
