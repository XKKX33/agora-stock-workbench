"""AI 接口边界与 OpenAI 兼容提供方。

本模块做两件事:
1. **定义边界**:让"AI 未配置"成为可查询、可展示的明确状态,而不是页面上
   一段写死的文案或一个悄悄返回空字符串的函数。
2. **实现一个真实提供方**:`openai_compatible`——任何遵循 OpenAI
   `/chat/completions` 协议的服务(OpenAI、DeepSeek、本地 vLLM/Ollama 网关等)
   都能接,只需在 settings.yaml 里给 base_url 和模型名。

三条硬约束(与之前一致):
1. 没有凭据就是没有。`describe()` 如实返回 availability="unconfigured"
   并说清缺什么,绝不返回编造的"AI 摘要"。
2. 未配置时调用 `generate()` 直接抛 `AIUnavailableError`,不静默降级成
   规则模板输出——那会让人以为看到的是模型结论。
3. 凭据只从环境变量读,settings.yaml 里只写变量名。配置文件会进版本库,
   密钥不能。

接入另一个真实提供方:实现 `ReviewNarrator` 协议并注册进 `NARRATOR_REGISTRY`,
与舆情采集器同构。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

try:
    import httpx
except ImportError:  # pragma: no cover - requirements 里固定有 httpx
    httpx = None  # type: ignore[assignment]


class AIConfigError(ValueError):
    """AI 配置本身有问题(字段缺失、类型不对、provider 未注册)。"""


class AIUnavailableError(RuntimeError):
    """AI 未配置或不可用时被调用。调用方应把它当成明确失败上报。"""


class AIRequestError(RuntimeError):
    """模型调用失败(网络 / HTTP 错误 / 返回不可解析)。

    与 AIUnavailableError 的区分:配置齐了但调用失败是真实故障,
    不是"没配置"。调用方要能区分两者,给出不同的错误提示。
    """


@dataclass(frozen=True)
class AIConfig:
    """AI 配置。凭据不落在这里,只记录去哪个环境变量取。

    enabled:      总开关。false 时无论凭据是否存在都不启用。
    provider:     提供方标识,须已在 NARRATOR_REGISTRY 注册。
    model:        模型名。留空表示未指定,不替用户挑默认模型。
    base_url:     OpenAI 兼容服务的 API 根地址(不含 /chat/completions)。
                  仅 openai_compatible 需要,必填——不猜默认地址。
    api_key_env:  凭据所在的环境变量名。
    temperature:  采样温度。默认 0.2,研判任务偏好稳定输出。
    max_tokens:   单次回答上限。
    """

    enabled: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: str = "WORKBENCH_AI_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 2000

    def api_key(self) -> Optional[str]:
        """读取凭据。空串按未配置处理——环境变量设成空值不算配置好了。"""
        return os.environ.get(self.api_key_env) or None


@runtime_checkable
class ReviewNarrator(Protocol):
    """复盘叙述器协议。接一个真实提供方时实现它即可。

    输入是 build_review 的返回值(已带 fact / derived / unverified 标注),
    输出是一段自然语言复盘。实现方**不得**新增事实:模型只能重述与串联
    已入库的内容,任何它自己"想出来"的数字都是幻觉。
    """

    def narrate(self, review: dict) -> str: ...


class OpenAICompatibleClient:
    """OpenAI 兼容接口的最小客户端。

    只依赖 httpx(requirements 已有)。兼容层要点:
    - base_url 由配置显式给出,绝不拼默认地址;
    - 凭据走 Authorization: Bearer;本地服务可能不需要 key,此时 api_key 为空
      也不拦——这是提供方差异,由调用方按 describe 的约定约束;
    - json_mode=True 时优先带 response_format,服务不支持会报 400,调用方
      可降级重试(提示词约束 JSON)。
    """

    def __init__(self, config: AIConfig) -> None:
        if httpx is None:
            raise AIUnavailableError("httpx 未安装,无法发起模型请求")
        self.config = config
        base = (config.base_url or "").rstrip("/")
        self.endpoint = f"{base}/chat/completions"
        self._client = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0))

    def chat(
        self,
        messages: List[dict],
        *,
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 2,
    ) -> str:
        """发起一次对话补全,返回模型文本。

        失败重试 2 次(连接错误 / 5xx),退避 1s/2s;4xx 不重试——
        那是请求本身的问题,重试不会变好,只会拖慢失败路径。
        """
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        api_key = self.config.api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                response = self._client.post(
                    self.endpoint, json=payload, headers=headers
                )
                if response.status_code >= 400:
                    raise AIRequestError(
                        f"模型接口返回 HTTP {response.status_code}: "
                        f"{response.text[:300]}"
                    )
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
            except AIRequestError:
                raise
            except Exception as error:  # noqa: BLE001 - 网络层故障统一重试
                last_error = error
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
        raise AIRequestError(f"模型调用连续失败: {last_error}") from last_error

    def close(self) -> None:
        self._client.close()


def build_openai_compatible(config: AIConfig) -> "OpenAICompatibleNarrator":
    """构造 openai_compatible 叙述器(并注册为 NARRATOR_REGISTRY 工厂)。"""
    return OpenAICompatibleNarrator(config)


class OpenAICompatibleNarrator:
    """openai_compatible 提供方的复盘叙述实现。"""

    def __init__(self, config: AIConfig) -> None:
        self.config = config
        self.client = OpenAICompatibleClient(config)

    def narrate(self, review: dict) -> str:
        system = (
            "你是 A 股盘后复盘分析师。只能重述输入 JSON 中已给出的事实与数字,"
            "不得编造、推断或补充任何输入里没有的内容。输入各节带 available 标记,"
            "不可用的小节明确说'该数据缺失',不要假装有数据。用简体中文,"
            "条理清晰,面向短线交易者。"
        )
        payload = json.dumps(review, ensure_ascii=False, default=str)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"复盘数据:\n{payload}\n\n请输出复盘。"},
        ]
        return self.client.chat(messages)


# 已实现的叙述器。空 dict 之前是有意的闸门;接入 openai_compatible 后,
# 任何未注册的 provider 仍会在 build_narrator 处明确报错。
NARRATOR_REGISTRY: Dict[str, Callable[["AIConfig"], "ReviewNarrator"]] = {
    "openai_compatible": build_openai_compatible,
}


def load_ai_config(settings: dict) -> AIConfig:
    """从 settings.yaml 的 ai 段构造配置。

    缺少 ai 段按"未启用"处理:这是明确的关闭状态,不是错误。但字段类型
    写错要立刻报错——静默忽略一个拼错的 provider,用户会以为 AI 开着。
    """
    raw: Any = (settings or {}).get("ai") or {}
    if not isinstance(raw, dict):
        raise AIConfigError(f"ai 段应为映射,收到 {type(raw).__name__}")

    provider = str(raw.get("provider") or "").strip() or None
    model = str(raw.get("model") or "").strip() or None
    base_url = str(raw.get("base_url") or "").strip() or None
    api_key_env = str(raw.get("api_key_env") or "").strip() or "WORKBENCH_AI_API_KEY"

    temperature = 0.2
    max_tokens = 2000
    if raw.get("temperature") is not None:
        temperature = float(raw["temperature"])
    if raw.get("max_tokens") is not None:
        max_tokens = int(raw["max_tokens"])

    return AIConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def describe(config: AIConfig) -> dict:
    """AI 可用性自述,供 API 与页面直接展示。

    availability 只有三种取值:
        disabled     —— 配置里明确关着
        unconfigured —— 开着但缺 provider / 凭据 / 实现
        available    —— 三样齐全,可以调用
    不设"部分可用":半配置状态下调用会失败,对用户就是不可用。
    """
    base = {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_key_env": config.api_key_env,
    }
    if not config.enabled:
        return {
            **base,
            "availability": "disabled",
            "reason": "settings.yaml 中 ai.enabled 为 false,未启用 AI",
        }

    missing = _missing_requirements(config)
    if missing:
        return {
            **base,
            "availability": "unconfigured",
            "reason": "AI 已启用但配置不完整:" + ";".join(missing),
            "missing": missing,
        }
    return {**base, "availability": "available", "reason": None}


def _missing_requirements(config: AIConfig) -> list[str]:
    """列出还缺什么。全部列齐,不在第一项就返回——一次说清好过来回试。"""
    missing: list[str] = []
    if not config.provider:
        missing.append("未指定 ai.provider")
    elif config.provider not in NARRATOR_REGISTRY:
        missing.append(
            f"provider={config.provider} 尚未实现"
            f"(已注册: {sorted(NARRATOR_REGISTRY) or '(无)'})"
        )
    if config.provider == "openai_compatible" and not config.base_url:
        missing.append("未指定 ai.base_url(OpenAI 兼容服务的 API 根地址)")
    if not config.api_key():
        missing.append(f"环境变量 {config.api_key_env} 未设置")
    if not config.model:
        missing.append("未指定 ai.model")
    return missing


def build_narrator(config: AIConfig) -> ReviewNarrator:
    """构造叙述器。任何一项不满足都抛错,不返回降级实现。"""
    info = describe(config)
    if info["availability"] != "available":
        raise AIUnavailableError(info["reason"] or "AI 不可用")
    factory = NARRATOR_REGISTRY[str(config.provider)]
    return factory(config)


def build_client(config: AIConfig) -> OpenAICompatibleClient:
    """构造底层对话客户端(供多 agent 研判引擎复用同一套接入)。"""
    info = describe(config)
    if info["availability"] != "available":
        raise AIUnavailableError(info["reason"] or "AI 不可用")
    if config.provider != "openai_compatible":
        raise AIConfigError(
            f"多 agent 研判目前只支持 openai_compatible,收到 {config.provider}"
        )
    return OpenAICompatibleClient(config)


def narrate_review(config: AIConfig, review: dict) -> str:
    """生成 AI 复盘叙述。未配置时抛 AIUnavailableError,绝不返回假文本。"""
    return build_narrator(config).narrate(review)


__all__ = [
    "NARRATOR_REGISTRY",
    "AIConfig",
    "AIConfigError",
    "AIUnavailableError",
    "AIRequestError",
    "ReviewNarrator",
    "OpenAICompatibleClient",
    "OpenAICompatibleNarrator",
    "build_client",
    "build_narrator",
    "build_openai_compatible",
    "describe",
    "load_ai_config",
    "narrate_review",
]
