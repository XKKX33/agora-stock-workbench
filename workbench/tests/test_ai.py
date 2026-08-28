"""AI 接口边界的单元测试。

核心断言只有一条:没配置就说没配置,绝不返回编造内容。

运行:
    cd workbench
    python -m pytest tests/test_ai.py -q
"""

from __future__ import annotations

import builtins
import json
import runpy
import sys
import types
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ai import (  # noqa: E402
    NARRATOR_REGISTRY,
    AIConfig,
    AIConfigError,
    AIRequestError,
    AIUnavailableError,
    OpenAICompatibleClient,
    build_narrator,
    describe,
    load_ai_config,
    narrate_review,
)
import engine.config as config_module  # noqa: E402
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


def test_workspace_env_loader_reads_dotenv_without_overriding_process_env(
    tmp_path, monkeypatch
):
    assert hasattr(config_module, "load_workspace_env")
    env_name = "WORKBENCH_DOTENV_TEST"
    (tmp_path / ".env").write_text(f"{env_name}=from-file\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.delenv(env_name, raising=False)

    config_module.load_workspace_env()
    assert config_module.os.environ[env_name] == "from-file"

    monkeypatch.setenv(env_name, "from-process")
    config_module.load_workspace_env()
    assert config_module.os.environ[env_name] == "from-process"


def test_runtime_dependencies_include_python_dotenv():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    declared = [
        line.split("#", 1)[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert any(line.startswith("python-dotenv") for line in declared)


def test_serve_loads_workspace_env_before_app_config_import(monkeypatch):
    env_name = "WORKBENCH_SERVE_ENV_ORDER_TEST"
    observed = []
    monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        config_module,
        "load_workspace_env",
        lambda: monkeypatch.setenv(env_name, "loaded"),
    )
    fake_app_config = types.ModuleType("app.config")
    fake_app_config.AppSettings = type("AppSettings", (), {})
    real_import = builtins.__import__

    def import_with_observation(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.config":
            observed.append(config_module.os.environ.get(env_name))
            return fake_app_config
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_with_observation)

    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "serve.py"),
        run_name="serve_env_test",
    )

    assert observed == ["loaded"]


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


def test_default_settings_use_supplied_provider_without_storing_secret():
    settings = load_settings()

    assert settings["ai"]["base_url"] == "https://cpa.xuan.christmas/v1"
    assert settings["ai"]["model"] == "minimax-m3"
    assert settings["ai"]["api_key_env"] == "WORKBENCH_AI_API_KEY"
    assert "api_key" not in settings["ai"]
    assert settings["agent"]["base_url"] == "https://cpa.xuan.christmas/v1"
    assert settings["agent"]["api_key_env"] == "WORKBENCH_AI_API_KEY"
    assert settings["agent"]["enabled"] is True
    assert settings["agent"]["reasoning_effort"] == "low"
    # max_tokens 是运营可调的输出预算:推理模型的思考 token 也计入输出,辩论
    # 角色还要带完整 transcript 逐条反驳,按实测调大调小属日常运营,具体数值
    # 不是契约。写死 1200 会让每次调参都误伤这条用例——而按用例名,它真正要
    # 守的是"出厂配置指向指定 provider 且不落盘明文密钥"。
    #
    # 保留下来的契约:出厂 agent 段必须能过设置接口自己声明的边界(当前
    # ge=100、le=32000)。越界才是真缺陷——UI 一保存就 422,PiModelConfig
    # 一构造就抛。用 AgentSettingsIn 复核而不是在测试里复写数字,边界日后
    # 调整时这条断言自动跟随,不会变成第二处需要同步的事实来源。
    from app.api.settings import AgentSettingsIn

    assert isinstance(settings["agent"]["max_tokens"], int)
    assert (
        AgentSettingsIn(**settings["agent"]).max_tokens
        == settings["agent"]["max_tokens"]
    )
    assert "api_key" not in settings["agent"]


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
            model="deepseek-v4-flash",
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
        "model": "deepseek-v4-flash",
        "authorized": True,
    }


def test_openai_client_sends_configured_reasoning_effort(monkeypatch):
    seen = {}

    def capture_ok(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    monkeypatch.setenv(ENV_KEY, "fake-api-key")
    client = OpenAICompatibleClient(
        AIConfig(
            enabled=True,
            provider="openai_compatible",
            model="grok-4.5",
            base_url="https://grok.xuan.christmas/v1",
            api_key_env=ENV_KEY,
            reasoning_effort="low",
        ),
        transport=httpx.MockTransport(capture_ok),
    )
    try:
        assert client.chat([], json_mode=True) == "{}"
    finally:
        client.close()

    assert seen["reasoning_effort"] == "low"

def test_chat_stream_yields_text_deltas_and_ignores_done(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "stream-test-secret")

    def stream_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
                'data: [DONE]\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/", api_key_env=ENV_KEY),
        transport=httpx.MockTransport(stream_response),
    )
    try:
        assert list(client.chat_stream([{"role": "user", "content": "ping"}], retries=0)) == ["hello", " world"]
    finally:
        client.close()


def test_chat_stream_does_not_retry_after_yielded_delta(monkeypatch):
    calls = 0

    class DeltaThenFailure(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            raise RuntimeError("transport failed")

    def stream_response(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            stream=DeltaThenFailure(),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr("engine.ai.time.sleep", lambda _seconds: None)
    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/"),
        transport=httpx.MockTransport(stream_response),
    )
    chunks = []
    try:
        with pytest.raises(AIRequestError, match="模型流调用连续失败"):
            for chunk in client.chat_stream([], retries=1):
                chunks.append(chunk)
    finally:
        client.close()

    assert chunks == ["hello"]
    assert calls == 1

def test_chat_stream_rejects_malformed_delta():
    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"choices":[]}\n\n')

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/"),
        transport=httpx.MockTransport(malformed),
    )
    try:
        with pytest.raises(AIRequestError, match="流响应格式错误"):
            list(client.chat_stream([], retries=0))
    finally:
        client.close()


def test_chat_stream_error_does_not_expose_key_or_body(monkeypatch):
    secret = "stream-secret-value"
    monkeypatch.setenv(ENV_KEY, secret)

    def failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"provider leaked {secret}")

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/", api_key_env=ENV_KEY),
        transport=httpx.MockTransport(failure),
    )
    try:
        with pytest.raises(AIRequestError) as captured:
            list(client.chat_stream([], retries=0))
    finally:
        client.close()
    assert secret not in str(captured.value)
def test_chat_stream_rejects_empty_non_terminal_delta():
    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/"),
        transport=httpx.MockTransport(malformed),
    )
    try:
        with pytest.raises(AIRequestError, match="流响应格式错误"):
            list(client.chat_stream([], retries=0))
    finally:
        client.close()


def test_chat_stream_accepts_terminal_empty_delta():
    def terminal(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":null}]}'
                "\n\n"
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                "\n\n"
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://grok.xuan.christmas/"),
        transport=httpx.MockTransport(terminal),
    )
    try:
        assert list(client.chat_stream([], retries=0)) == ["{}"]
    finally:
        client.close()


def test_chat_stream_accepts_usage_chunk_after_finish():
    def terminal_usage(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"{}"}}]}'
                "\n\n"
                'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":"stop"}]}'
                "\n\n"
                'data: {"choices":[],"usage":{"completion_tokens":10}}'
                "\n\n"
                "data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible", model="m", base_url="https://api.pie-xian.com/v1"),
        transport=httpx.MockTransport(terminal_usage),
    )
    try:
        assert list(client.chat_stream([], retries=0)) == ["{}"]
    finally:
        client.close()



def test_configured_default_endpoint_is_grok():
    settings = load_settings()
    assert settings["ai"]["base_url"] == "https://cpa.xuan.christmas/v1"
    assert settings["agent"]["base_url"] == "https://cpa.xuan.christmas/v1"
    assert settings["ai"]["model"] == "minimax-m3"


def test_openai_http_error_never_exposes_provider_response_body(monkeypatch):
    sentinel = "Bearer AUDIT_SECRET_SENTINEL"

    def echo_secret(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "sk-provider-token-123",
                    "message": f"请求头包含 {sentinel}",
                }
            },
        )

    monkeypatch.setenv(ENV_KEY, "fake-api-key")
    client = OpenAICompatibleClient(
        AIConfig(
            enabled=True,
            provider="openai_compatible",
            model="deepseek-v4-flash",
            base_url="https://api.pie-xian.com/v1",
            api_key_env=ENV_KEY,
        ),
        transport=httpx.MockTransport(echo_secret),
    )

    try:
        with pytest.raises(AIRequestError) as captured:
            client.chat([{"role": "user", "content": "ping"}], retries=0)
    finally:
        client.close()

    message = str(captured.value)
    assert "HTTP 400" in message
    assert "sk-provider-token-123" not in message
    assert sentinel not in message
    assert "AUDIT_SECRET_SENTINEL" not in message


def test_openai_client_retries_server_errors(monkeypatch):
    calls = 0

    def recover_on_third_call(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(
                503,
                json={"error": {"code": "temporarily_unavailable"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setenv(ENV_KEY, "fake-api-key")
    monkeypatch.setattr("engine.ai.time.sleep", lambda _seconds: None)
    client = OpenAICompatibleClient(
        AIConfig(
            enabled=True,
            provider="openai_compatible",
            model="deepseek-v4-flash",
            base_url="https://api.pie-xian.com/v1",
            api_key_env=ENV_KEY,
        ),
        transport=httpx.MockTransport(recover_on_third_call),
    )

    try:
        assert client.chat([{"role": "user", "content": "ping"}], retries=2) == "ok"
    finally:
        client.close()
    assert calls == 3


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
