"""AI 接口边界的单元测试。

核心断言只有一条:没配置就说没配置,绝不返回编造内容。

运行:
    cd workbench
    python -m pytest tests/test_ai.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ai import (  # noqa: E402
    NARRATOR_REGISTRY,
    AIConfig,
    AIConfigError,
    AIUnavailableError,
    OpenAICompatibleClient,
    build_narrator,
    describe,
    load_ai_config,
    narrate_review,
)
from engine.config import load_settings  # noqa: E402
import app.services.ai as ai_service_module  # noqa: E402

pytestmark = pytest.mark.unit

ENV_KEY = "WORKBENCH_AI_TEST_KEY"


@pytest.fixture(autouse=True)
def clean_registry():
    """用例结束后还原 registry,避免注册的假 provider 泄漏到别的测试。"""
    saved = dict(NARRATOR_REGISTRY)
    yield
    NARRATOR_REGISTRY.clear()
    NARRATOR_REGISTRY.update(saved)


# ---------------------------------------------------------------- 配置解析


def test_missing_ai_section_is_disabled_not_error():
    """缺 ai 段是明确的关闭状态,不是配置错误。"""
    config = load_ai_config({})
    assert config.enabled is False
    assert describe(config)["availability"] == "disabled"


def test_non_mapping_ai_section_raises():
    """写错类型要立刻报错,不能静默忽略。"""
    with pytest.raises(AIConfigError):
        load_ai_config({"ai": ["oops"]})


def test_blank_fields_become_none():
    """空串按未指定处理,不当成一个名叫""的 provider。"""
    config = load_ai_config({"ai": {"enabled": True, "provider": "  ", "model": ""}})
    assert config.provider is None
    assert config.model is None


def test_api_key_env_defaults():
    config = load_ai_config({"ai": {}})
    assert config.api_key_env == "WORKBENCH_AI_API_KEY"


def test_default_settings_enable_deepseek_without_storing_secret():
    settings = load_settings()

    assert settings["ai"] == {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://api.pie-xian.com/v1",
        "api_key_env": "WORKBENCH_AI_API_KEY",
        "model": "deepseekv4flash",
    }
    assert settings["agent"]["enabled"] is True
    assert "api_key" not in settings["ai"]


def test_ai_service_uses_local_settings(monkeypatch):
    merged = {
        "ai": {
            "enabled": False,
            "provider": "openai_compatible",
            "base_url": "https://local.example/v1",
            "api_key_env": ENV_KEY,
            "model": "local-model",
        }
    }
    monkeypatch.setattr(ai_service_module, "load_settings_with_local", lambda: merged)

    service = ai_service_module.AIService(repository=None)

    assert service.config.model == "local-model"
    assert service.config.base_url == "https://local.example/v1"


# ---------------------------------------------------------------- 可用性自述


def test_disabled_takes_precedence_over_credentials(monkeypatch):
    """总开关关着时,即使凭据齐全也是 disabled。"""
    monkeypatch.setenv(ENV_KEY, "sk-test")
    config = AIConfig(enabled=False, provider="fake", model="m", api_key_env=ENV_KEY)
    assert describe(config)["availability"] == "disabled"


def test_enabled_without_credentials_is_unconfigured(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    config = AIConfig(enabled=True, provider="fake", model="m", api_key_env=ENV_KEY)
    info = describe(config)
    assert info["availability"] == "unconfigured"
    assert any(ENV_KEY in item for item in info["missing"])


def test_empty_env_var_counts_as_unset(monkeypatch):
    """环境变量设成空串不算配置好了。"""
    monkeypatch.setenv(ENV_KEY, "")
    config = AIConfig(enabled=True, provider="fake", model="m", api_key_env=ENV_KEY)
    assert describe(config)["availability"] == "unconfigured"


def test_unregistered_provider_is_reported(monkeypatch):
    """provider 没实现要点名说,不能含糊成"配置不完整"。"""
    monkeypatch.setenv(ENV_KEY, "sk-test")
    config = AIConfig(enabled=True, provider="nope", model="m", api_key_env=ENV_KEY)
    info = describe(config)
    assert info["availability"] == "unconfigured"
    assert any("nope" in item for item in info["missing"])


def test_all_missing_requirements_listed_at_once(monkeypatch):
    """缺三样就列三条,不要让人一次修一个来回试。"""
    monkeypatch.delenv(ENV_KEY, raising=False)
    config = AIConfig(enabled=True, provider=None, model=None, api_key_env=ENV_KEY)
    assert len(describe(config)["missing"]) == 3


def test_registry_has_openai_compatible():
    """闸门检查:目前唯一注册的提供方是 openai_compatible。"""
    assert set(NARRATOR_REGISTRY) == {"openai_compatible"}


# ---------------------------------------------------------------- 调用行为


def test_openai_client_uses_deepseek_request_contract(monkeypatch):
    seen = {}

    def capture_ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(
            url=str(request.url),
            model=body["model"],
            authorized=request.headers.get("Authorization") == "Bearer fake-api-key",
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setenv(ENV_KEY, "fake-api-key")
    client = OpenAICompatibleClient(
        AIConfig(
            enabled=True,
            provider="openai_compatible",
            model="deepseekv4flash",
            base_url="https://api.pie-xian.com/v1",
            api_key_env=ENV_KEY,
        ),
        transport=httpx.MockTransport(capture_ok),
    )

    try:
        assert client.chat([{"role": "user", "content": "ping"}]) == "ok"
    finally:
        client.close()

    assert seen == {
        "url": "https://api.pie-xian.com/v1/chat/completions",
        "model": "deepseekv4flash",
        "authorized": True,
    }


def test_narrate_without_config_raises_not_fake_text(monkeypatch):
    """未配置时抛错,绝不返回一段看起来像 AI 输出的文本。"""
    monkeypatch.delenv(ENV_KEY, raising=False)
    config = AIConfig(enabled=True, provider="fake", model="m", api_key_env=ENV_KEY)
    with pytest.raises(AIUnavailableError):
        narrate_review(config, {"sections": {}})


def test_disabled_narrate_raises(monkeypatch):
    config = AIConfig(enabled=False, api_key_env=ENV_KEY)
    with pytest.raises(AIUnavailableError):
        build_narrator(config)


def test_registered_provider_becomes_available(monkeypatch):
    """三样齐全时才 available,并且真的能拿到叙述器。"""
    monkeypatch.setenv(ENV_KEY, "sk-test")

    class _Echo:
        def narrate(self, review: dict) -> str:
            return f"共 {len(review['sections'])} 节"

    NARRATOR_REGISTRY["fake"] = lambda _config: _Echo()
    config = AIConfig(enabled=True, provider="fake", model="m", api_key_env=ENV_KEY)

    assert describe(config)["availability"] == "available"
    assert narrate_review(config, {"sections": {"a": {}, "b": {}}}) == "共 2 节"
