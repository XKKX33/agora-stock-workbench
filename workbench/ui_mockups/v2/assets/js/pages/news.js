import { query, request } from "/assets/js/api.js";
import { clearError, initShell, setLoading, setStatus, showError } from "/assets/js/app-shell.js";
import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";

initShell("news");

// 情绪与任务状态的中文展示，null 一律按「未判定」处理，不与「中性」混淆
const SENTIMENT = {
  positive: ["正面", "good"],
  negative: ["负面", "bad"],
  neutral: ["中性", "active"],
  undecided: ["未判定", "pending"],
};
const KIND_LABEL = { notice: "公告", news: "新闻", research: "研报" };
const JOB_STATUS = {
  succeeded: ["成功", "good"],
  failed: ["失败", "bad"],
  running: ["运行中", "active"],
  queued: ["排队中", "pending"],
};

const tradeDateInput = document.querySelector("#trade-date");
const collectBtn = document.querySelector("#collect-btn");
const collectState = document.querySelector("#collect-state");

// URL 参数 ?trade_date=YYYYMMDD 可指定日期
const urlTradeDate = (new URLSearchParams(location.search).get("trade_date") || "").trim();
if (urlTradeDate) tradeDateInput.value = urlTradeDate;

// 舆情列表最近一次真实返回，情绪概览读不到扫描批次时用它兜底，绝不编造
let lastDigest = null;
// 当前选中的行业板块；null 表示看全部
let activeIndustry = null;

function sentimentTag(value) {
  const [label, kind] = SENTIMENT[value] || SENTIMENT.undecided;
  return statusTag(label, kind);
}

function jobStatusTag(status) {
  const [label, kind] = JOB_STATUS[status] || [String(status || "未知"), "pending"];
  return statusTag(label, kind);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ---------------------------------------------------------------- 情绪概览
async function loadOverview() {
  try {
    const data = await request("/api/sentiment");
    const stage = data.market_stage || {};
    document.querySelector("#metric-stage").textContent = stage.label || "算不出";
    document.querySelector("#metric-stage-note").textContent =
      stage.passed_ratio == null
        ? stage.reason || "来自最近一次扫描批次"
        : `门槛通过率 ${formatPercent(stage.passed_ratio)}`;
    renderNewsCards(data.news_sentiment || lastDigest);
  } catch (error) {
    // 情绪概览依赖扫描批次，读不到就如实显示原因，不编造
    document.querySelector("#metric-stage").textContent = "—";
    document.querySelector("#metric-stage-note").textContent = error?.message || "情绪概览依赖最近一次扫描批次";
    renderNewsCards(lastDigest);
  }
}

// 兼容两套真实返回：/api/sentiment 的 news_sentiment 与 /api/news 的 digest
function renderNewsCards(payload) {
  const stateEl = document.querySelector("#metric-news-state");
  const noteEl = document.querySelector("#metric-news-note");
  const sampleEl = document.querySelector("#metric-sample");
  const sampleNoteEl = document.querySelector("#metric-sample-note");
  const coverageEl = document.querySelector("#metric-coverage");
  const coverageNoteEl = document.querySelector("#metric-coverage-note");
  const unavailable = !payload || payload.availability === "unavailable" || payload.available === false;
  if (unavailable) {
    stateEl.textContent = "未接入";
    stateEl.className = "metric-value warning";
    noteEl.textContent = payload?.detail || payload?.missing_reason || "暂无舆情数据";
    sampleEl.textContent = "—";
    sampleNoteEl.textContent = "情绪为规则推断，未人工核验";
    coverageEl.textContent = formatDate(payload?.coverage?.latest);
    coverageNoteEl.textContent = payload?.coverage?.earliest
      ? `覆盖 ${formatDate(payload.coverage.earliest)} ~ ${formatDate(payload.coverage.latest)}`
      : "无覆盖区间";
    return;
  }
  let counts = payload.counts;
  if (!counts) {
    counts = { positive: 0, negative: 0, neutral: 0, undecided: 0 };
    (payload.items || []).forEach((item) => {
      const label = item.judgement?.sentiment || "undecided";
      counts[label] = (counts[label] || 0) + 1;
    });
  }
  const sample = payload.sample_count ?? (payload.items || []).length;
  stateEl.textContent = "已接入";
  stateEl.className = "metric-value positive";
  noteEl.textContent = `交易日 ${formatDate(payload.trade_date)}`;
  sampleEl.textContent = `${formatNumber(sample, 0)} 条`;
  sampleNoteEl.textContent =
    `正面 ${formatNumber(counts.positive, 0)} · 负面 ${formatNumber(counts.negative, 0)} · 中性 ${formatNumber(counts.neutral, 0)} · 未判定 ${formatNumber(counts.undecided, 0)}`;
  const coverage = payload.coverage || {};
  coverageEl.textContent = formatDate(coverage.latest);
  coverageNoteEl.textContent = coverage.earliest
    ? `覆盖 ${formatDate(coverage.earliest)} ~ ${formatDate(coverage.latest)}`
    : "无覆盖区间";
}

// ---------------------------------------------------------------- 今日舆情
async function loadDigest() {
  try {
    const params = { limit: 50, trade_date: tradeDateInput.value.trim() };
    // 选了板块就走单行业接口（带匹配依据），没选就走当日全量列表
    const data = activeIndustry
      ? await query(`/api/news/industries/${encodeURIComponent(activeIndustry)}`, params)
      : await query("/api/news", params);
    lastDigest = data;
    renderDigest(data);
  } catch (error) {
    showError(error);
    document.querySelector("#news-list").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderDigest(data) {
  const metaEl = document.querySelector("#digest-meta");
  const stateEl = document.querySelector("#digest-state");
  const listEl = document.querySelector("#news-list");
  if (!data.available) {
    // 板块下钻时"采过但该板块没关联上"是正常态，不引导去采集
    if (activeIndustry && data.missing_reason === "no_linked_news") {
      metaEl.textContent = `板块 ${activeIndustry} · 无关联`;
      stateEl.innerHTML = statusTag("无关联", "pending");
      listEl.innerHTML = `
        <div class="empty-state news-empty">
          <span class="reason">no_linked_news</span>
          <span class="detail">${escapeHtml(data.detail || "该板块今天没有关联新闻")}</span>
        </div>`;
      return;
    }
    metaEl.textContent = data.trade_date ? `${formatDate(data.trade_date)} · 未接入` : "未接入";
    stateEl.innerHTML = statusTag("未接入", "pending");
    listEl.innerHTML = `
      <div class="empty-state news-empty">
        <span class="reason">${escapeHtml(data.missing_reason || "unknown")}</span>
        <span class="detail">${escapeHtml(data.detail || "暂无舆情数据")}</span>
        <button id="empty-collect" class="button primary">一键采集</button>
      </div>`;
    document.querySelector("#empty-collect")?.addEventListener("click", startCollect);
    return;
  }
  metaEl.textContent = activeIndustry
    ? `板块 ${activeIndustry} · ${data.items.length} 条关联`
    : `${formatDate(data.trade_date)} · ${data.items.length} 条`;
  stateEl.innerHTML = statusTag("有数据", "good");
  listEl.innerHTML = data.items.map(renderItem).join("");
}

function renderItem(item) {
  const source = item.source || {};
  const judgement = item.judgement || {};
  const link = item.link || {};
  const kindLabel = KIND_LABEL[source.kind] || source.kind;
  const title = item.url
    ? `<a class="news-title" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>`
    : `<span class="news-title">${escapeHtml(item.title)}</span>`;
  return `
    <article class="news-item">
      <div class="news-top">${title}${sentimentTag(judgement.sentiment)}</div>
      ${item.summary ? `<p class="news-summary">${escapeHtml(item.summary)}</p>` : ""}
      <div class="news-meta">
        <span>${escapeHtml(source.name || "未知来源")}${kindLabel ? ` · ${escapeHtml(kindLabel)}` : ""}</span>
        <span class="mono">${formatDate(item.published_at)}</span>
        ${link.match_text ? `<span>命中 "${escapeHtml(link.match_text)}"</span>` : ""}
        ${judgement.event_type ? `<span>事件 ${escapeHtml(judgement.event_type)}</span>` : ""}
        ${judgement.credibility != null ? `<span>可信度 ${formatNumber(judgement.credibility, 2)}</span>` : ""}
      </div>
    </article>`;
}

// ---------------------------------------------------------------- 行业板块
async function loadIndustries() {
  try {
    const data = await query("/api/news/industries", { trade_date: tradeDateInput.value.trim() });
    renderIndustryBar(data);
  } catch (error) {
    document.querySelector("#industry-bar").innerHTML = `<span class="industry-empty">板块加载失败：${escapeHtml(error.message)}</span>`;
  }
}

function renderIndustryBar(data) {
  const bar = document.querySelector("#industry-bar");
  if (!data.available) {
    bar.innerHTML = `<span class="industry-empty">${escapeHtml(data.detail || data.missing_reason || "暂无板块数据")}</span>`;
    return;
  }
  const allChip = `
    <button type="button" class="industry-chip${activeIndustry ? "" : " active"}" data-industry="ALL">全部</button>`;
  const chips = (data.industries || []).map((group) => {
    const s = group.sentiment || {};
    const undecided = (s.undecided || 0) > 0 ? `<span class="und">未${formatNumber(s.undecided, 0)}</span>` : "";
    return `
      <button type="button" class="industry-chip${activeIndustry === group.industry ? " active" : ""}" data-industry="${escapeHtml(group.industry)}">
        ${escapeHtml(group.industry)}
        <span class="cnt">${formatNumber(group.news_count, 0)} 条</span>
        <span class="pos">正${formatNumber(s.positive || 0, 0)}</span>
        <span class="neg">负${formatNumber(s.negative || 0, 0)}</span>
        <span class="mid">中${formatNumber(s.neutral || 0, 0)}</span>
        ${undecided}
      </button>`;
  }).join("");
  // 没匹配到行业的新闻如实单列，不硬塞进任何板块
  const unlinked = data.unlinked_count > 0
    ? `<span class="industry-note">另有 ${formatNumber(data.unlinked_count, 0)} 条未匹配到行业</span>`
    : "";
  bar.innerHTML = allChip + chips + unlinked;
  bar.querySelectorAll(".industry-chip").forEach((el) => {
    el.addEventListener("click", () => {
      const next = el.dataset.industry === "ALL" ? null : el.dataset.industry;
      if (next === activeIndustry) return;
      activeIndustry = next;
      bar.querySelectorAll(".industry-chip").forEach((chip) => chip.classList.toggle("active", chip === el));
      loadDigest();
    });
  });
}

// ---------------------------------------------------------------- 来源登记
async function loadSources() {
  try {
    const data = await request("/api/news/sources");
    renderSources(data);
  } catch (error) {
    showError(error);
    document.querySelector("#sources-box").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderSources(data) {
  const box = document.querySelector("#sources-box");
  if (!data.available || !data.items.length) {
    box.innerHTML = `<div class="empty-state">${escapeHtml(data.detail || "尚未登记任何舆情来源")}</div>`;
    return;
  }
  box.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>名称</th><th>类型</th><th>可信度</th><th>合规备注</th></tr></thead>
        <tbody>${data.items.map((source) => `
          <tr>
            <td>${source.home_url
              ? `<a class="source-link" href="${escapeHtml(source.home_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.name)}</a>`
              : escapeHtml(source.name)}</td>
            <td>${escapeHtml(KIND_LABEL[source.kind] || source.kind || "—")}</td>
            <td class="mono">${source.base_credibility == null ? "—" : formatNumber(source.base_credibility, 2)}</td>
            <td>${escapeHtml(source.compliance_note || "—")}</td>
          </tr>`).join("")}</tbody>
      </table>
    </div>`;
}

// ---------------------------------------------------------------- 采集历史
async function loadHistory() {
  try {
    const data = await request("/api/news/collect/jobs");
    const rows = data.items || [];
    document.querySelector("#history-rows").innerHTML = rows.map((job) => `
      <tr>
        <td class="mono">${escapeHtml((job.job_id || job.task_id || "").slice(0, 8))}…</td>
        <td class="mono">${formatDate(job.trade_date)}</td>
        <td>${jobStatusTag(job.status)}</td>
        <td class="mono">${formatTime(job.created_at)}</td>
        <td class="mono">${formatTime(job.finished_at)}</td>
      </tr>`).join("") || `<tr><td colspan="5"><div class="empty-state">还没有采集任务</div></td></tr>`;
  } catch (error) {
    showError(error);
    document.querySelector("#history-rows").innerHTML = `<tr><td colspan="5"><div class="empty-state">${escapeHtml(error.message)}</div></td></tr>`;
  }
}

// ---------------------------------------------------------------- 一键采集
async function startCollect() {
  clearError();
  collectBtn.disabled = true;
  collectState.textContent = "正在排队…";
  try {
    const tradeDate = tradeDateInput.value.trim();
    // 409（采集未启用）/ 400（日期非法）等真实原因由 request 抛出，showError 原样展示
    const job = await request("/api/news/collect", {
      method: "POST",
      body: JSON.stringify(tradeDate ? { trade_date: tradeDate } : {}),
    });
    collectState.textContent = `任务 ${job.job_id.slice(0, 8)}… 采集中`;
    await pollCollect(job.job_id);
    setStatus("舆情采集完成", "ready");
    await loadDigest();
    await loadIndustries();
    await loadSources();
    await loadHistory();
    await loadOverview();
  } catch (error) {
    showError(error);
    setStatus("舆情采集失败", "error");
  } finally {
    collectBtn.disabled = false;
    collectState.textContent = "";
  }
}

async function pollCollect(jobId) {
  while (true) {
    const job = await request(`/api/news/collect/${jobId}`);
    if (job.status === "succeeded") return job;
    if (job.status === "failed") throw new Error(job.error?.message || "舆情采集失败");
    const [label] = JOB_STATUS[job.status] || [String(job.status || "处理中"), "pending"];
    collectState.textContent = `任务 ${jobId.slice(0, 8)}… ${label}`;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

tradeDateInput.addEventListener("change", () => {
  clearError();
  loadDigest();
  loadIndustries();
  loadOverview();
});
collectBtn.addEventListener("click", startCollect);

// 情绪概览依赖舆情列表兜底，先取列表再刷新概览
loadDigest().then(loadOverview);
loadIndustries();
loadSources();
loadHistory();