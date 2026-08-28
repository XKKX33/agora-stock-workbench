import { escapeHtml } from "/assets/js/format.js";

const STATUS_LABELS = { queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败" };
const STEP_LABELS = {
  preflight: "配置预检", market_data: "市场数据", candidate_pool: "候选池", integrity: "完整性校验", score: "综合评分",
  prepare: "准备", fetch_sources: "抓取来源", normalize: "归一化", deduplicate: "去重", link: "关联板块和股票", persist: "写入数据库", complete: "完成",
};

export function createTaskPanel(root, { title = "任务进度" } = {}) {
  if (!root) throw new Error("任务面板容器不存在");
  root.innerHTML = `
    <section class="task-panel" aria-label="${escapeHtml(title)}">
      <div class="task-panel-head">
        <div><h2>${escapeHtml(title)}</h2><p data-task-meta>尚未提交任务</p></div>
        <span class="status-tag muted" data-task-status>空闲</span>
      </div>
      <div class="task-progress-row">
        <div class="task-progress-track" role="progressbar" aria-label="任务进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div data-task-bar></div></div>
        <span class="mono" data-task-percent>0%</span>
      </div>
      <p class="task-message" data-task-message>点击按钮后显示后台真实进度。</p>
      <ol class="task-steps" data-task-steps></ol>
      <div class="task-log" aria-live="polite"><div class="task-log-title">运行日志</div><div data-task-logs class="task-log-list"><div class="empty-state">暂无日志</div></div></div>
      <div class="task-summary" data-task-summary hidden></div>
    </section>`;

  const el = (selector) => root.querySelector(selector);
  const summaryText = (task) => {
    if (task.status === "failed") return `任务失败 · ${task.error?.message || "后端返回失败"}`;
    const result = task.result || {};
    if (result.stored != null) return `采集完成 · 新增 ${result.stored} 条 · 去重 ${result.duplicates || 0} 条 · 关联 ${result.links || 0} 条`;
    if (result.candidate_count != null) return `任务完成 · 候选 ${result.candidate_count} 只 · 通过 ${result.passed_count || 0} 只`;
    return "任务完成";
  };

  function update(task) {
    if (!task) return;
    const result = task.result || {};
    const progress = result.progress || {};
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || (task.status === "succeeded" ? 100 : 0)));
    const bar = el("[data-task-bar]");
    const track = el("[role=progressbar]");
    if (bar) bar.style.width = `${percent}%`;
    if (track) track.setAttribute("aria-valuenow", String(percent));
    el("[data-task-percent]").textContent = `${percent}%`;
    el("[data-task-status]").textContent = STATUS_LABELS[task.status] || task.status || "未知";
    el("[data-task-status]").className = `status-tag ${task.status === "failed" ? "bad" : task.status === "succeeded" ? "good" : task.status === "running" ? "active" : "muted"}`;
    el("[data-task-meta]").textContent = task.job_id ? `任务 ${String(task.job_id).slice(0, 8)} · ${task.trade_date || "未指定交易日"}` : "尚未提交任务";
    el("[data-task-message]").textContent = progress.message || task.error?.message || "等待后台状态";
    el("[data-task-steps]").innerHTML = (result.steps || []).map((step) => `<li class="task-step ${escapeHtml(step.status || "waiting")}"><span>${step.status === "succeeded" ? "完成" : step.status === "running" ? "运行" : step.status === "failed" ? "失败" : "等待"}</span><strong>${escapeHtml(STEP_LABELS[step.name] || step.name || "未命名步骤")}</strong><small>${escapeHtml(step.detail || "")}</small></li>`).join("");
    const logs = progress.logs || [];
    const logHost = el("[data-task-logs]");
    logHost.innerHTML = logs.length ? logs.map((log) => `<div class="task-log-line ${escapeHtml(log.level || "info")}"><time>${escapeHtml(String(log.at || "").slice(11, 19))}</time><span><b>${escapeHtml(log.message || "")}</b>${log.detail && log.detail !== log.message ? `<small>${escapeHtml(log.detail)}</small>` : ""}</span></div>`).join("") : `<div class="empty-state">暂无日志</div>`;
    logHost.scrollTop = logHost.scrollHeight;
    const summary = el("[data-task-summary]");
    if (task.status === "succeeded" || task.status === "failed") {
      summary.hidden = false;
      summary.textContent = summaryText(task);
    }
  }

  function reset() {
    update({ status: "queued", result: { progress: { percent: 0, message: "等待后台状态", logs: [] }, steps: [] } });
    el("[data-task-summary]").hidden = true;
  }

  return { update, reset };
}
