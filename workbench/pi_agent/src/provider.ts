import { Agent, type StreamFn } from "@earendil-works/pi-agent-core";
import { contentText, type Model } from "@earendil-works/pi-ai";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";

export interface ProviderResponse { text: string; inputTokens?: number; outputTokens?: number; finishReason?: string }
export interface RoleProvider { complete(role: string, prompt: string, signal: AbortSignal): Promise<ProviderResponse> }

export function jsonResponseFormat(): { type: "json_object" } { return { type: "json_object" }; }

export function roleSystemPrompt(role: string): string {
  if (["methodology", "sentiment", "technical"].includes(role)) {
    return `You are the ${role} stock analyst. Return JSON only and match this exact schema: {"stance":"bull|bear|neutral","conclusion":"string","risks":["string"]}. stance must be exactly bull, bear, or neutral. Do not add markdown fences or extra fields.`;
  }
  return `You are the ${role} stock analyst. Return JSON only with up to the requested number of valid selected candidates, using this schema: {"selected":[{"ts_code":"string","reason":"string"}]}. risk_control is handled by the system. Do not add markdown fences or extra fields.`;
}

export class FauxProvider implements RoleProvider {
  async complete(role: string, prompt: string, signal: AbortSignal): Promise<ProviderResponse> {
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    const parsed = JSON.parse(prompt) as Record<string, any>;
    const code = parsed.ts_code ?? "000000.SZ";
    if (role === "coarse") {
      const candidates = Array.isArray(parsed.candidates) ? parsed.candidates : [parsed];
      const limit = Number(parsed.limit ?? 1);
      const selected = candidates.slice().sort((a, b) => Number(b.score ?? 0) - Number(a.score ?? 0)).slice(0, limit).map((candidate) => ({ ts_code: candidate.ts_code, score: Number(candidate.score ?? 50), reason: "候选评分" }));
      return { text: JSON.stringify({ selected }), inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    }
    if (["methodology", "sentiment", "technical"].includes(role)) {
      return { text: JSON.stringify({ stance: role === "sentiment" ? "neutral" : "bull", conclusion: `${role}分析`, risks: [] }), inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    }
    if (role === "debate") {
      const candidates = Array.isArray(parsed.candidates) ? parsed.candidates : [];
      const limit = Number(parsed.limit ?? 3);
      return { text: JSON.stringify({ selected: candidates.slice(0, limit).map((item) => ({ ts_code: item.ts_code, decision: "hold", score: Number(item.score ?? 50), bull_case: "增长", bear_case: "波动", rebuttal: "已反驳", risk_control: "控制仓位" })) }), inputTokens: 1, outputTokens: 2, finishReason: "stop" };
    }
    return { text: JSON.stringify({ decision: "hold", score: Number(parsed.score ?? 50), bull_case: "增长", bear_case: "波动", rebuttal: "已反驳", risk_control: "控制仓位" }), inputTokens: 1, outputTokens: 2, finishReason: "stop" };
  }
}

export interface PiAgentAdapterOptions { model?: Model<any> }
export class PiAgentProvider implements RoleProvider {
  private readonly model: Model<"openai-completions">;
  private readonly apiKey: string;
  constructor(private readonly options: PiAgentAdapterOptions = {}) {
    this.apiKey = process.env.PI_AGENT_API_KEY ?? process.env.MINIMAX_API_KEY ?? "";
    this.model = (options.model as Model<"openai-completions"> | undefined) ?? {
      id: process.env.PI_AGENT_MODEL ?? "minimax-m3", name: "MiniMax M3", api: "openai-completions", provider: "openai-compatible",
      baseUrl: process.env.PI_AGENT_BASE_URL ?? "https://api.pie-xian.com/v1", reasoning: true, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32768, maxTokens: 8192,
      compat: { supportsReasoningEffort: true, maxTokensField: "max_tokens", supportsUsageInStreaming: true }
    };
  }

  async complete(role: string, prompt: string, signal: AbortSignal): Promise<ProviderResponse> {
    if (!this.apiKey) throw new Error("PI_AGENT_API_KEY is required");
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    const agent = new Agent({
      initialState: { systemPrompt: roleSystemPrompt(role), model: this.model, thinkingLevel: "low", tools: [] },
      streamFn: ((model: Model<any>, context, options) => streamSimple(model as Model<"openai-completions">, context, {
        ...options,
        samplingParams: { ...(options?.samplingParams ?? {}), response_format: jsonResponseFormat() }
      })) as StreamFn,
      getApiKey: () => this.apiKey,
      toolExecution: "sequential"
    });
    const onAbort = () => agent.abort(); signal.addEventListener("abort", onAbort, { once: true });
    try {
      await agent.prompt(prompt);
      const message = agent.state.messages.at(-1);
      if (!message || message.role !== "assistant") throw new Error("Pi agent returned no assistant message");
      return { text: contentText(message.content), inputTokens: message.usage.input, outputTokens: message.usage.output, finishReason: message.stopReason };
    } finally { signal.removeEventListener("abort", onAbort); }
  }
}
