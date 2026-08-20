// p11 is the launch/configuration page; p13 owns live/history report rendering.
import { initShell, setStatus, showError, workContextParams } from "/assets/js/app-shell.js";
import { query, request } from "/assets/js/api.js";
import { escapeHtml, formatNumber } from "/assets/js/format.js";

initShell("agents");

const statusHost = document.querySelector("#agent-status");
const progress = document.querySelector("#agent-progress");
const stageHost = document.querySelector("#agent-stage");
const stepHost = document.querySelector("#agent-step");
const bar = document.querySelector("#agent-bar");
const messageHost = document.querySelector("#agent-message");
const resultsHost = document.querySelector("#agent-results");
const recentHost = document.querySelector("#agent-recent");
const poolNote = document.querySelector("#agent-pool-note");
const modeTitle = document.querySelector("#mode-title");
const modeDesc = document.querySelector("#mode-desc");
const singleFields = document.querySelector("#single-fields");
const flowFields = document.querySelector("#flow-fields");

let agentDefaults = { candidates: 200, depth: 8, final: 3 };
let agentLimits = { max_candidates: 200, max_depth: 30, max_final: 10 };
let mode = "single";
let pollTimer = null;

const AGENT_LS = "hermes.agent.params";

function readParams() {
  const saved = JSON.parse(localStorage.getItem(AGENT_LS) || "{}");
  document.querySelector("#agent-candidates").value = saved.candidates ?? agentDefaults.candidates;
  document.querySelector("#agent-depth").value = saved.depth ?? agentDefaults.depth;
  document.querySelector("#agent-final").value = saved.final ?? agentDefaults.final;
}

function saveParams() {
  localStorage.setItem(AGENT_LS, JSON.stringify({
    candidates: Number(document.querySelector("#agent-candidates").value) || agentDefaults.candidates,
    depth: Number(document.querySelector("#agent-depth").value) || agentDefaults.depth,
    final: Number(document.querySelector("#agent-final").value) || agentDefaults.final,
  }));
}

function clampParams() {
  const c = document.querySelector("#agent-candidates");
  const d = document.querySelector("#agent-depth");
  const f = document.querySelector("#agent-final");
  c.value = Math.max(1, Math.min(Number(c.value) || 1, agentLimits.max_candidates));
  d.value = Math.max(1, Math.min(Number(d.value) || 1, agentLimits.max_depth, Number(c.value)));
  f.value = Math.max(1, Math.min(Number(f.value) || 1, agentLimits.max_final, Number(d.value)));
}

function renderStatus(info) {
  if (!statusHost) return;
  const availability = info?.availability || "disabled";
  const reason = info?.reason || "";
  if (availability === "available") {
    statusHost.innerHTML = `<span class="tag good">已配置</span> ${esc(info?.model || "openai_compatible")}`;
  } else {
    const label = availability === "disabled" ? "未启用" : availability === "unconfigured" ? "配置不完整" : "不可用";
    statusHost.innerHTML = `<span class="tag">${label}</span> ${esc(reason)}`;
  }
  document.querySelector("#agent-single-run").disabled = availability !== "available";
  document.querySelector("#agent-flow-run").disabled = availability !== "available";
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function stanceText(stance) {
  return { bullish: "看多", neutral: "中性", bearish: "看空" }[stance] || stance || "—";
}

function verdictClass(verdict) {
  if (verdict === "看多") return "verdict-bull";
  if (verdict === "看空") return "verdict-bear";
  return "verdict-flat";
}

function renderAnalyst(name, analyst) {
  if (!analyst) return "";
  return `<div class="agent-analyst"><b>${esc(name)}</b> <span class="mono">${formatNumber(analyst.score, 0)} · ${esc(stanceText(analyst.stance))}</span>
    <div style="margin-top:4px">${(analyst.points || []).map((pt) => "· " + esc(pt)).join("<br>")}</div>
    ${(analyst.risks || []).length ? `<div style="color:#c49a4a;margin-top:3px">${analyst.risks.map((r) => "! " + esc(r)).join("<br>")}</div>` : ""}
  </div>`;
}

function renderJudgeResults(job) {
  if (!resultsHost) return;
  const items = job.judgments || [];
  if (!items.length) { resultsHost.innerHTML = `<div class="agent-empty">本次研判没有输出结论</div>`; return; }
  resultsHost.innerHTML = items.map((item) => {
    const stage = item.stage || {};
    const deep = stage.deep || {};
    const debate = stage.debate || {};
    const analysts = deep.analysts || {};
    return `
      <article class="agent-card" data-code="${esc(item.ts_code)}">
        <div class="agent-card-head">
          <strong>${esc(item.name || "—")}</strong>
          <span class="mono">${esc(item.ts_code)}</span>
          <span class="mono muted">排名 ${esc(Number(item.rank) || 0)}</span>
          <span class="spacer"></span>
          <span class="tag ${verdictClass(stage.verdict)}">${esc(stage.verdict || "未定")}</span>
        </div>
        <div class="agent-score-row"><span class="mono muted">综合 ${formatNumber(item.score, 0)}</span>
          <span class="agent-score-track"><span class="agent-score-fill" style="width:${Math.max(0, Math.min(100, Number(item.score) || 0))}%"></span></span>
          <span class="mono muted">${esc(stanceText(item.stance))}</span>
        </div>
        <div class="agent-thesis">${esc(item.thesis || "—")}</div>
        <div class="agent-action">操作建议：${esc(stage.action || "—")}</div>
        ${(item.risks || []).length ? `<ul class="agent-risks">${item.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        <div class="agent-source">数据来源：选股台扫描 / 日线·周线指标 / TrendRadar 舆情 / 资金流</div>
        <div class="agent-card-foot">
          <button type="button" class="button primary" data-act="agent-watch" style="min-height:30px;padding:0 12px;font-size:12px">☆ 加入自选</button>
          <a class="button" href="p6_chart.html?code=${encodeURIComponent(item.ts_code)}" style="min-height:30px;padding:6px 12px;font-size:12px">看K线</a>
        </div>
        <details class="agent-detail">
          <summary>分析师详情与多空辩论</summary>
          ${renderAnalyst("方法论", analysts.methodology)}
          ${renderAnalyst("舆情", analysts.sentiment)}
          ${renderAnalyst("走势", analysts.trend)}
          ${debate.bull || debate.bear ? `<div class="agent-debate"><div><b>多方：</b>${esc(debate.bull || "—")}</div><div class="bear"><b>空方：</b>${esc(debate.bear || "—")}</div></div>` : ""}
        </details>
      </article>`;
  }).join("");
  resultsHost.querySelectorAll("[data-act='agent-watch']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".agent-card");
      if (!card) return;
      const code = card.dataset.code;
      try {
        await request("/api/watchlist", {
          method: "POST",
          body: JSON.stringify({ ts_code: code }),
        });
        btn.textContent = "★ 已自选";
        btn.disabled = true;
        setStatus("已加入自选", "ready");
      } catch (error) {
        showError(error);
      }
    });
  });
}

function renderProgress(stage, step, total, msg) {
  if (!progress) return;
  progress.hidden = false;
  if (stage === "done") {
    stageHost.innerHTML = "完成";
    bar.style.width = "100%";
  } else {
    stageHost.innerHTML = stage || "运行中";
    stepHost.textContent = total ? `${step} / ${total}` : "";
    bar.style.width = total ? `${Math.round((step / total) * 100)}%` : "30%";
  }
  messageHost.textContent = msg || "";
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollJob(jobId) {
  stopPoll();
  pollTimer = setInterval(async () => {
    try {
      const job = await request(`/api/agents/jobs/${encodeURIComponent(jobId)}`);
      if (job?.progress) {
        renderProgress(job.progress.stage, job.progress.step, job.progress.total, job.progress.message);
      }
      if (job?.status === "succeeded") {
        stopPoll();
        renderProgress("done", 1, 1, "研判完成");
        renderJudgeResults(job);
        loadRecent();
      } else if (job?.status === "failed") {
        stopPoll();
        renderProgress("failed", 0, 0, job.error?.message || "研判失败");
        resultsHost.innerHTML = "";
      }
    } catch (error) {
      showError(error);
    }
  }, 1500);
}

async function startSingle() {
  const code = document.querySelector("#agent-ts-code").value.trim().toUpperCase();
  if (!code) { showError(new Error("请输入股票代码")); return; }
  const force = document.querySelector("#agent-force-single").checked;
  resultsHost.innerHTML = "";
  try {
    const job = await request("/api/agents/single", {
      method: "POST",
      body: JSON.stringify({ ...workContextParams(), ts_code: code, force }),
    });
    renderProgress("queued", 0, 0, "排队中");
    pollJob(job.job_id);
  } catch (error) { showError(error); }
}

async function startFlow() {
  const body = {
    ...workContextParams(),
    candidates: Number(document.querySelector("#agent-candidates").value) || agentDefaults.candidates,
    depth: Number(document.querySelector("#agent-depth").value) || agentDefaults.depth,
    final: Number(document.querySelector("#agent-final").value) || agentDefaults.final,
    force: document.querySelector("#agent-force-flow").checked,
  };
  resultsHost.innerHTML = "";
  try {
    const job = await request("/api/agents/judge", { method: "POST", body: JSON.stringify(body) });
    renderProgress("queued", 0, 0, "排队中");
    await pollJob(job.job_id);
  } catch (error) { showError(error); }
}

function switchMode(next) {
  mode = next;
  const isSingle = mode === "single";
  singleFields.hidden = !isSingle;
  flowFields.hidden = isSingle;
  modeTitle.textContent = isSingle ? "单股深度研判" : "Agent 选股研判";
  modeDesc.textContent = isSingle
    ? "针对一只股票运行分析、辩论与风控"
    : "从候选池粗筛，经多角色分析和风控输出最终名单";
  document.querySelector("#mode-single").classList.toggle("active", isSingle);
  document.querySelector("#mode-flow").classList.toggle("active", !isSingle);
}

async function loadRecent() {
  if (!recentHost) return;
  try {
    const data = await request("/api/agents/jobs?limit=6");
    const items = data.items || [];
    recentHost.innerHTML = items.length
      ? `<div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">最近研判 · 详情与 SSE 报告见 p13</div>` + items.map((it) => `
        <span class="agent-recent-row" data-job="${esc(it.task_id || it.job_id || it.run_id || "")}">
          ${esc(it.strategy || it.kind || "")} · ${esc(it.status || "")}
        </span>`).join("")
      : `<div style="font-size:12px;color:var(--text-muted)">暂无研判记录 · 可从 p13 打开历史报告</div>`;
    recentHost.querySelectorAll("[data-job]").forEach((row) => {
      row.addEventListener("click", async () => {
        const id = row.dataset.job;
        try {
          const job = await request(`/api/agents/jobs/${encodeURIComponent(id)}`);
          renderJudgeResults(job);
        } catch (error) { showError(error); }
      });
    });
  } catch (error) { showError(error); }
}

async function loadStatus() {
  try {
    const info = await request("/api/agents/status");
    agentDefaults = info.defaults || agentDefaults;
    agentLimits = info.limits || agentLimits;
    const cand = document.querySelector("#agent-candidates");
    const dep = document.querySelector("#agent-depth");
    const fin = document.querySelector("#agent-final");
    cand.max = agentLimits.max_candidates ?? 200;
    dep.max = agentLimits.max_depth ?? 30;
    fin.max = agentLimits.max_final ?? 10;
    readParams();
    renderStatus(info);
    const pool = await request("/api/agents/candidates?limit=200");
    if (poolNote) poolNote.textContent = `当前候选池 ${pool.items?.length || 0} 只 · 截面 ${pool.as_of || "—"}`;
    renderStockOptions(pool.items || []);
  } catch (error) { showError(error); }
}


function renderStockOptions(items) {
  const box = document.querySelector("#agent-stock-options");
  if (!box) return;
  box.innerHTML = items.map((item) => `<option value="${esc(item.ts_code)}">${esc(item.name || "")} · ${esc(item.industry || "")}</option>`).join("");
}

document.querySelector("#mode-single")?.addEventListener("click", () => switchMode("single"));
document.querySelector("#mode-flow")?.addEventListener("click", () => switchMode("flow"));
document.querySelector("#agent-single-run")?.addEventListener("click", startSingle);
document.querySelector("#agent-flow-run")?.addEventListener("click", startFlow);
document.querySelector("#agent-ts-code")?.addEventListener("keydown", (e) => { if (e.key === "Enter") startSingle(); });
[document.querySelector("#agent-candidates"), document.querySelector("#agent-depth"), document.querySelector("#agent-final")].forEach((el) => el?.addEventListener("change", clampParams));

loadStatus().then(loadRecent).catch(showError);
