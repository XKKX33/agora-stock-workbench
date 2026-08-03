import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("ledger");

async function load() {
  clearError(); setLoading(true);
  try {
    const strategy = document.querySelector("#strategy").value;
    const [ledger, summary] = await Promise.all([query("/api/ledger", { strategy, per_page: 200 }), query("/api/ledger/summary", { strategy })]);
    renderSummary(summary);
    renderRows(ledger.items || []);
    setStatus(`${summary.total} 条选股记录`, "ready");
  } catch (error) { showError(error); } finally { setLoading(false); }
}

function renderSummary(summary) {
  document.querySelector("#ledger-total").textContent = formatNumber(summary.total, 0);
  ["ret1", "ret3", "ret5", "ret10"].forEach((key) => {
    const item = summary[key];
    document.querySelector(`#${key}-avg`).textContent = item.average === null ? "—" : formatPercent(item.average);
    document.querySelector(`#${key}-sample`).textContent = `${item.sample_count} 个样本`;
  });
}

function renderRows(items) {
  document.querySelector("#ledger-rows").innerHTML = items.map((item) => `
    <tr><td class="mono">${escapeHtml(item.run_date)}</td><td><strong>${escapeHtml(item.name)}</strong><br><span class="mono muted">${escapeHtml(item.ts_code)}</span></td><td>${escapeHtml(item.industry)}</td><td class="mono">${formatNumber(item.total, 4)}</td><td>${statusTag(item.money_class || "未确认")}</td>${["ret1", "ret3", "ret5", "ret10"].map((key) => { const v = item[key]; const cls = v == null ? "muted" : v > 0 ? "positive" : v < 0 ? "negative" : "muted"; return `<td class="mono ${cls}">${v == null ? "待回填" : formatPercent(v)}</td>`; }).join("")}</tr>`).join("") || `<tr><td colspan="9"><div class="empty-state">暂无选股台账</div></td></tr>`;
}

document.querySelector("#strategy")?.addEventListener("change", load);
document.querySelector("#refresh")?.addEventListener("click", load);
load();
