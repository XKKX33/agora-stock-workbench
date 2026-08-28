from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from app.schemas.pi_agent import (
    PiAgentRequest,
    PiLimits,
    PiMethodology,
    PiModelConfig,
    compute_candidate_hash,
    compute_input_hash,
    validate_judgment_result,
)
from engine.methodology import build_agent_brief
from app.services.pi_agent import PiAgentClient, PiAgentProcessSupervisor, PiAgentProtocolError


pytestmark = pytest.mark.unit


@pytest.fixture
def free_url() -> str:
    """给 supervisor.start() 一个当前空闲的端口。

    supervisor 启动前会真的探一次端口,写死 43123 会撞上本机正在跑的 Pi Agent,
    让测试结果取决于开发机有没有开服务。这里现取一个空闲端口,测试自己隔离。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _request(*, mode: str = "batch") -> PiAgentRequest:
    candidates = [
        {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行"},
        {"ts_code": "000002.SZ", "name": "万科A", "industry": "地产"},
    ]
    snapshots = [
        {"ts_code": code, "stock": {"ts_code": code}, "daily": {}}
        for code in ("000001.SZ", "000002.SZ")
    ]
    return PiAgentRequest(
        protocol_version="1",
        workflow_version="1",
        mode=mode,
        trade_date="2026-08-13",
        candidate_hash=compute_candidate_hash(candidates),
        input_hash=compute_input_hash(candidates, snapshots),
        limits=PiLimits(coarse=2, deep=2, final=1),
        candidates=candidates,
        snapshots=snapshots,
        model=PiModelConfig(
            provider="openai-compatible",
            model="minimax-m3",
            reasoning_effort="low",
            max_tokens=8192,
        ),
        # 用生产代码同一个装配函数：方法论正文与角色职责的唯一来源是
        # engine.methodology，测试自己拼一份就测不到两侧不一致。
        methodology=PiMethodology(**build_agent_brief()),
    )


def _result(request: PiAgentRequest) -> dict:
    analysts = {
        "methodology": {"stance": "bull", "conclusion": "方法通过", "risks": []},
        "sentiment": {"stance": "neutral", "conclusion": "舆情中性", "risks": []},
        "trend": {"stance": "bull", "conclusion": "趋势向上", "risks": ["波动"]},
    }
    return {
        "protocol_version": "1",
        "workflow_version": "1",
        "run_id": "run-1",
        "trade_date": request.trade_date,
        "candidate_hash": request.candidate_hash,
        "input_hash": request.input_hash,
        "coarse": [
            {"ts_code": "000001.SZ", "rank": 1, "score": 80, "reason": "资金确认"},
            {"ts_code": "000002.SZ", "rank": 2, "score": 70, "reason": "趋势稳定"},
        ],
        "deep": [
            {"ts_code": "000001.SZ", "rank": 1, "score": 78, "analysts": analysts},
            {"ts_code": "000002.SZ", "rank": 2, "score": 68, "analysts": analysts},
        ],
        "final": [
            {
                "ts_code": "000001.SZ",
                "rank": 1,
                "decision": "buy",
                "score": 79,
                "bull_case": "资金持续流入",
                "bear_case": "指数回撤",
                "rebuttal": "设置止损后风险可控",
                "risk_control": "跌破支撑止损",
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def test_hashes_are_stable_and_distinguish_input():
    candidates = [{"b": 2, "a": 1}]
    snapshots = [{"ts_code": "000001.SZ", "value": None}]
    assert compute_candidate_hash(candidates) == compute_candidate_hash([{ "a": 1, "b": 2 }])
    assert len(compute_input_hash(candidates, snapshots)) == 64
    assert compute_input_hash(candidates, snapshots) != compute_input_hash(candidates, [{"ts_code": "000001.SZ", "value": 1}])
def test_hashes_normalize_integral_float_like_javascript_json():
    integer = [{"ts_code": "000001.SZ", "close": 10}]
    floating = [{"ts_code": "000001.SZ", "close": 10.0}]
    assert compute_input_hash(integer, integer) == compute_input_hash(floating, floating)


def test_result_validation_accepts_valid_subset_and_counts():
    request = _request()
    validated = validate_judgment_result(_result(request), request, "run-1")
    assert validated.run_id == "run-1"
    assert [item.ts_code for item in validated.final] == ["000001.SZ"]


def test_result_validation_allows_coarse_output_up_to_coarse_limit():
    request = _request().model_copy(
        update={"limits": PiLimits(coarse=2, deep=1, final=1)}
    )
    result = _result(request)
    result["deep"] = result["deep"][:1]

    validated = validate_judgment_result(result, request, "run-1")

    assert len(validated.coarse) == 2
    assert len(validated.deep) == 1


def test_result_validation_rejects_invalid_subset():
    request = _request()
    result = _result(request)
    result["final"][0]["ts_code"] = "999999.SZ"
    with pytest.raises(PiAgentProtocolError, match="subset"):
        validate_judgment_result(result, request, "run-1")


def test_result_validation_rejects_non_finite_score_and_bad_ranks():
    request = _request()
    result = _result(request)
    result["deep"][0]["score"] = float("inf")
    with pytest.raises(PiAgentProtocolError, match="score"):
        validate_judgment_result(result, request, "run-1")

    result = _result(request)
    result["coarse"][1]["rank"] = 3
    with pytest.raises(PiAgentProtocolError, match="rank"):
        validate_judgment_result(result, request, "run-1")


def test_result_validation_rejects_missing_analyst_fields():
    request = _request()
    result = _result(request)
    del result["deep"][0]["analysts"]["trend"]["conclusion"]
    with pytest.raises(PiAgentProtocolError, match="trend"):
        validate_judgment_result(result, request, "run-1")
def test_result_validation_rejects_candidate_and_input_hash_mismatch():
    request = _request()
    result = _result(request)
    result["candidate_hash"] = "0" * 64
    with pytest.raises(PiAgentProtocolError, match="candidate_hash"):
        validate_judgment_result(result, request, "run-1")

    result = _result(request)
    result["input_hash"] = "f" * 64
    with pytest.raises(PiAgentProtocolError, match="input_hash"):
        validate_judgment_result(result, request, "run-1")


def test_client_uses_bearer_and_supports_all_endpoints_and_sse():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"protocol_version": "1", "status": "ready"})
        if request.method == "PUT":
            return httpx.Response(202, json={"run_id": "run-1", "status": "queued"})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-1", "status": "cancelled"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, text='id: 1\nevent: run.started\ndata: {"run_id": "run-1"}\n\ndata: [DONE]\n\n', headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"run_id": "run-1"})
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    client = PiAgentClient("http://127.0.0.1:43123", "secret-token", transport=httpx.MockTransport(handler))
    assert client.health()["status"] == "ready"
    assert client.create_run("run-1", {"x": 1})["status"] == "queued"
    assert client.status("run-1")["status"] == "running"
    assert client.result("run-1")["run_id"] == "run-1"
    assert client.cancel("run-1")["status"] == "cancelled"
    event = list(client.events("run-1"))[0]
    assert event["event_type"] == "run.started"
    assert event["source_seq"] == 1
    assert all(request.headers.get("authorization") == "Bearer secret-token" for request in seen)
    client.close()
def test_client_formal_runner_contract_aliases_and_validated_result():
    request = _request()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            return httpx.Response(202, json={"run_id": "run-1", "status": "queued"})
        if req.url.path.endswith("/result"):
            return httpx.Response(200, json=_result(request))
        if req.url.path.endswith("/events"):
            return httpx.Response(200, text="id: 1\\nevent: run.started\\ndata: {}\\n\\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    client = PiAgentClient("http://127.0.0.1:43123", "token", transport=httpx.MockTransport(handler))
    assert client.start_judgment(request) == "run-1"
    assert client.get_status("run-1")["status"] == "running"
    assert list(client.stream_events("run-1", after_seq=1)) == []
    assert client.get_result("run-1", request).run_id == "run-1"
    client.close()


def test_client_redacts_token_from_errors():
    token = "api-key-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"upstream saw {token}")

    client = PiAgentClient("http://127.0.0.1:43123", token, transport=httpx.MockTransport(handler))
    with pytest.raises(PiAgentProtocolError) as error:
        client.health()
    assert token not in str(error.value)
    assert "REDACTED" in str(error.value)
    client.close()
def test_client_includes_redacted_error_body_for_http_failures():
    client = PiAgentClient(
        "http://127.0.0.1:43123",
        "secret-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"error": "bad request"})),
    )
    with pytest.raises(PiAgentProtocolError, match="bad request"):
        client.health()
    client.close()




def test_process_supervisor_exposes_safe_start_contract(tmp_path: Path, monkeypatch, free_url: str):
    calls: list[tuple[list[str], dict[str, str], Path]] = []

    class Process:
        pid = 123

        def poll(self):
            return None

    class Client:
        def __init__(self, base_url: str, _token: str, **_kwargs):
            self.base_url = base_url

        def health(self):
            return {"protocol_version": "1", "status": "ready"}

        def close(self):
            pass

    def factory(command: list[str], env: dict[str, str], cwd: Path):
        calls.append((command, env, cwd))
        return Process()

    monkeypatch.setattr("app.services.pi_agent.PiAgentClient", Client)
    supervisor = PiAgentProcessSupervisor(tmp_path, process_factory=factory, model_api_key_env="MODEL_KEY")
    handle = supervisor.start(base_url=free_url, model_api_key="hidden")
    assert calls
    command, env, cwd = calls[0]
    assert cwd == tmp_path
    assert "src/main.ts" in " ".join(command)
    assert "--port" in command and free_url.rsplit(":", 1)[1] in command
    assert env["PATH"]
    assert env["SYSTEMROOT"]
    assert env["PI_AGENT_INTERNAL_TOKEN"] == handle.token
    assert env["MODEL_KEY"] == "hidden"
    assert handle.client.base_url == free_url


def test_process_supervisor_accepts_valid_health_without_optional_status(tmp_path: Path, monkeypatch, free_url: str):
    class Process:
        pid = 123

        def poll(self):
            return None

    class Client:
        def __init__(self, base_url: str, _token: str, **_kwargs):
            self.base_url = base_url

        def health(self):
            return {
                "protocol_version": "1",
                "pi_version": "0.84.1",
                "workflow_version": "1",
                "running": False,
            }

        def close(self):
            pass

    monkeypatch.setattr("app.services.pi_agent.PiAgentClient", Client)
    supervisor = PiAgentProcessSupervisor(
        tmp_path,
        process_factory=lambda *_args: Process(),
        readiness_timeout=0.0,
    )

    handle = supervisor.start(
        base_url=free_url,
        model_api_key="hidden",
        internal_token="new-token",
    )

    assert handle.client.base_url == free_url


def test_process_supervisor_rejects_child_that_exits_before_ready(tmp_path: Path, monkeypatch, free_url: str):
    calls: list[str] = []

    class Process:
        pid = 123

        def poll(self):
            calls.append("poll")
            return 1

        def terminate(self):
            calls.append("terminate")

    class Client:
        def __init__(self, base_url: str, _token: str, **kwargs):
            self.base_url = base_url
            # start() 现在造两个客户端：业务客户端按业务超时（一个真实角色要跑几十秒），
            # 就绪探测客户端短超时快速失败重试。两者超时口径不同，用它区分身份，
            # 才能断言"两个客户端都被释放"，而不是笼统数一共 close 了几次。
            self.label = "probe" if kwargs.get("timeout") == supervisor.readiness_probe_timeout else "client"

        def health(self):
            calls.append("health")
            raise PiAgentProtocolError("Pi Agent HTTP 401")

        def close(self):
            calls.append(f"{self.label}.close")

    monkeypatch.setattr("app.services.pi_agent.PiAgentClient", Client)
    supervisor = PiAgentProcessSupervisor(tmp_path, process_factory=lambda *_args: Process())

    with pytest.raises(RuntimeError, match="启动后退出"):
        supervisor.start(
            base_url=free_url,
            model_api_key="hidden",
            internal_token="new-token",
        )

    # 新契约：失败路径上必须释放全部两个 httpx 客户端，否则漏掉的那个会带着
    # 连接池一起泄漏。业务客户端随 handle.close() 走（先关连接再终止子进程），
    # 探测客户端在 finally 里关——它是 start() 的局部资源，成功路径也一样要关。
    assert calls == ["poll", "client.close", "terminate", "probe.close"]
    assert supervisor.handle is None


def test_process_supervisor_closes_child_when_readiness_times_out(tmp_path: Path, monkeypatch, free_url: str):
    calls: list[str] = []

    class Process:
        pid = 123

        def poll(self):
            calls.append("poll")
            return None

        def terminate(self):
            calls.append("terminate")

    class Client:
        def __init__(self, base_url: str, _token: str, **kwargs):
            self.base_url = base_url
            self.label = "probe" if kwargs.get("timeout") == supervisor.readiness_probe_timeout else "client"

        def health(self):
            calls.append("health")
            raise PiAgentProtocolError("Pi Agent HTTP 401")

        def close(self):
            calls.append(f"{self.label}.close")

    class Clock:
        def __init__(self):
            self.values = iter((0.0, 11.0))

        def monotonic(self):
            return next(self.values)

        def sleep(self, _seconds: float):
            calls.append("sleep")

    import app.services.pi_agent as pi_agent_service

    monkeypatch.setattr(pi_agent_service, "PiAgentClient", Client)
    monkeypatch.setattr(pi_agent_service, "time", Clock(), raising=False)
    supervisor = PiAgentProcessSupervisor(tmp_path, process_factory=lambda *_args: Process())

    with pytest.raises(RuntimeError, match="就绪超时"):
        supervisor.start(
            base_url=free_url,
            model_api_key="hidden",
            internal_token="new-token",
        )

    # 同上：就绪超时也是失败路径，两个客户端都得释放。
    assert calls == ["poll", "health", "client.close", "terminate", "probe.close"]
    assert supervisor.handle is None


def test_process_handle_close_is_idempotent_and_closes_client_before_process(tmp_path: Path):
    calls: list[str] = []

    class Client:
        def close(self):
            calls.append("client")

    class Process:
        def terminate(self):
            calls.append("process")

    from app.services.pi_agent import PiAgentProcessHandle

    handle = PiAgentProcessHandle(Process(), Client(), "token", "http://127.0.0.1:1")
    handle.close()
    handle.close()
    assert calls == ["client", "process"]
