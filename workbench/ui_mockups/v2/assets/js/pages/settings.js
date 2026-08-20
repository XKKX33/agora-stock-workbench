// API 设置页:读取并保存 settings.local.yaml。
import { initShell, showError } from "/assets/js/app-shell.js";
import { request } from "/assets/js/api.js";

initShell("settings");

const status = document.querySelector("#save-status");

function set(id, value) {
  const el = document.querySelector(id);
  if (el && value !== undefined && value !== null) el.value = value;
}

async function loadSettings() {
  try {
    const data = await request("/api/settings");
    const agent = data.agent || {};
    set("#agent-enabled", agent.enabled ? "true" : "false");
    set("#agent-provider", agent.provider || "openai_compatible");
    set("#agent-base-url", agent.base_url || "");
    set("#agent-api-key-env", "WORKBENCH_AI_API_KEY");
    set("#agent-model", agent.model || "");
    set("#agent-temperature", agent.temperature ?? 0.2);
    set("#agent-max-tokens", agent.max_tokens ?? 4000);
    set("#agent-default-candidates", agent.default_candidates ?? 200);
    set("#agent-default-depth", agent.default_depth ?? 8);
    set("#agent-default-final", agent.default_final ?? 3);
    set("#agent-max-candidates", agent.max_candidates ?? 200);
    set("#agent-max-depth", agent.max_depth ?? 30);
    set("#agent-max-final", agent.max_final ?? 10);
    const avail = document.querySelector("#api-key-status");
    if (avail) {
      avail.textContent = data.api_key_available
        ? "环境变量已检测到,可用"
        : "环境变量尚未设置,保存后仍会被视为未配置";
    }
  } catch (error) { showError(error); }
}

async function saveSettings() {
  try {
    const payload = {
      agent: {
        enabled: document.querySelector("#agent-enabled").value === "true",
        provider: document.querySelector("#agent-provider").value.trim() || null,
        base_url: document.querySelector("#agent-base-url").value.trim() || null,
        api_key_env: "WORKBENCH_AI_API_KEY",
        model: document.querySelector("#agent-model").value.trim() || null,
        temperature: Number(document.querySelector("#agent-temperature").value) || null,
        max_tokens: Number(document.querySelector("#agent-max-tokens").value) || null,
        default_candidates: Number(document.querySelector("#agent-default-candidates").value) || null,
        default_depth: Number(document.querySelector("#agent-default-depth").value) || null,
        default_final: Number(document.querySelector("#agent-default-final").value) || null,
        max_candidates: Number(document.querySelector("#agent-max-candidates").value) || null,
        max_depth: Number(document.querySelector("#agent-max-depth").value) || null,
        max_final: Number(document.querySelector("#agent-max-final").value) || null,
      },
    };
    const result = await request("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    if (status) {
      status.textContent = `已保存到 ${result.saved ? "settings.local.yaml" : "本地设置"}，下次发起研判时立即生效`;
      status.className = "save-status ok";
    }
  } catch (error) {
    if (status) { status.textContent = `保存失败: ${error?.message || error}`; status.className = "save-status err"; }
    showError(error);
  }
}

document.querySelector("#settings-save")?.addEventListener("click", saveSettings);
loadSettings();
