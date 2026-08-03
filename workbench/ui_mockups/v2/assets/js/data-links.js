import { request } from "/assets/js/api.js";
import { escapeHtml } from "/assets/js/format.js";

// 数据链路三态（舆情/复盘/AI），每个接口要么有数据(good)、要么空(pending)、
// 要么未配置(muted)；接口本身挂掉才用 error，并显示真实原因，不编造。

export async function newsState() {
  try {
    const data = await request("/api/news");
    if (data.available) {
      return { kind: "good", label: `舆情 ${data.items.length} 条`, detail: `${data.trade_date || ""} 已采集` };
    }
    const reasons = {
      no_source_registered: "尚未登记舆情来源",
      never_collected: "已登记来源，但从未采集",
      no_news_on_date: "该交易日没有舆情条目",
    };
    return {
      kind: "pending",
      label: "舆情未接入",
      detail: data.detail || reasons[data.missing_reason] || data.missing_reason || "暂无舆情数据",
    };
  } catch (error) {
    return { kind: "error", label: "舆情不可用", detail: error?.message || String(error) };
  }
}

export async function reviewState() {
  try {
    const data = await request("/api/reviews");
    const sections = data.sections || {};
    const available = (data.available_sections || []).length;
    const total = Object.keys(sections).length;
    if (available >= total && total > 0) {
      return { kind: "good", label: "复盘已生成", detail: `${data.trade_date} ${total} 节全部可用` };
    }
    if (available > 0) {
      return { kind: "active", label: "复盘部分生成", detail: `${data.trade_date} 可用 ${available}/${total} 节` };
    }
    const reason = (data.missing || []).map((item) => item.detail).join("；");
    return { kind: "pending", label: "复盘待生成", detail: reason || "尚无复盘数据" };
  } catch (error) {
    return { kind: "error", label: "复盘不可用", detail: error?.message || String(error) };
  }
}

export async function aiState() {
  try {
    const data = await request("/api/ai/status");
    if (data.availability === "available") {
      return { kind: "good", label: "AI 已配置", detail: `${data.provider || ""} ${data.model || ""}`.trim() || "模型可用" };
    }
    if (data.availability === "disabled") {
      return { kind: "muted", label: "AI 未启用", detail: data.reason || "设置中 ai.enabled 为 false" };
    }
    return {
      kind: "muted",
      label: "AI 未配置",
      detail: data.reason || (data.missing || []).join("；") || "缺少模型或凭据",
    };
  } catch (error) {
    return { kind: "error", label: "AI 不可用", detail: error?.message || String(error) };
  }
}

// 把三个链路状态渲染进容器。容器内每行：状态点 + 名称 + 状态文字，悬浮显示原因。
export async function refreshDataLinks(container, names) {
  if (!container) return;
  const fetchers = [newsState, reviewState, aiState];
  const states = await Promise.all(fetchers.map((fn) => fn()));
  container.innerHTML = (names || ["舆情", "复盘", "AI 复盘"]).map((name, index) => {
    const state = states[index];
    return `<div class="data-link" title="${escapeHtml(state.detail)}">
      <span class="data-dot ${state.kind}"></span>
      <span class="data-link-name">${escapeHtml(name)}</span>
      <span class="data-link-state ${state.kind}">${escapeHtml(state.label)}</span>
    </div>`;
  }).join("");
}
