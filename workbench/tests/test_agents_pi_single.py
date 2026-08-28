from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.errors import WorkbenchError
from app.schemas.pi_agent import (
    PiAgentRequest,
    PiLimits,
    PiMethodology,
    PiModelConfig,
    compute_candidate_hash,
    compute_input_hash,
)
from engine.methodology import build_agent_brief
from app.services import agents as agents_service
from app.services.agents import AgentJudgeManager


pytestmark = pytest.mark.unit


def _single_request() -> PiAgentRequest:
    candidates = [{"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行"}]
    snapshots = [{"ts_code": "000001.SZ", "stock": {"ts_code": "000001.SZ"}}]
    return PiAgentRequest(
        protocol_version="1",
        workflow_version="1",
        mode="single",
        trade_date="20260813",
        candidate_hash=compute_candidate_hash(candidates),
        input_hash=compute_input_hash(candidates, snapshots),
        limits=PiLimits(coarse=1, deep=1, final=1),
        candidates=candidates,
        snapshots=snapshots,
        model=PiModelConfig(provider="test", model="fake", max_tokens=100),
        methodology=PiMethodology(**build_agent_brief()),
    )


def test_start_single_rejects_pi_unavailable_even_when_old_ai_is_configured(monkeypatch):
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager._pi_agent_status = {"availability": "unavailable", "reason": "Pi 未启动"}
    manager._pi_agent_client = None
    # 用公开注入口，不去手写私有属性：__new__ 绕过了 __init__，私有字段一个都没初始化。
    # 这里没有 supervisor（生产里 Pi 启动失败就是这个形态），传 None 表示"无人可复检"。
    manager.set_pi_supervisor(None)
    monkeypatch.setattr(
        manager,
        "_configs",
        lambda: (SimpleNamespace(enabled=True), SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(agents_service, "agent_status", lambda *_: {"availability": "available"})
    monkeypatch.setattr(manager, "_resolve_as_of", lambda: "20260813")

    with pytest.raises(WorkbenchError) as excinfo:
        manager.start_single(ts_code="000001.SZ")

    assert excinfo.value.code == "pi_agent_unavailable"
    assert excinfo.value.status_code == 503
    # details 是契约的一部分，不是可选装饰：前端拿 error.details 直接渲染不可用原因，
    # 不会为一次失败再打一遍 /api/agents/status。漏了它前端只能显示空白。
    assert excinfo.value.details == {"availability": "unavailable", "reason": "Pi 未启动"}


def test_run_single_uses_pi_client_and_never_old_engine(monkeypatch):
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager._pi_agent_status = {"availability": "available"}
    manager.set_pi_supervisor(None)
    request = _single_request()
    frozen = SimpleNamespace(
        candidates=request.candidates,
        snapshots=request.snapshots,
        candidate_hash=request.candidate_hash,
        input_hash=request.input_hash,
    )

    class Tracker:
        def mark_running(self, task_id):
            calls.append(("running", task_id))

        def finish(self, task_id, **kwargs):
            calls.append(("finish", task_id, kwargs))

        def now(self):
            return "2026-08-13T00:00:00+00:00"

    manager.db_path = "unused.duckdb"

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def update_agent_run(self, *args, **kwargs):
            calls.append(("update", args, kwargs))

    class Result:
        def model_dump(self, **_kwargs):
            return {
                "final": [
                    {
                        "ts_code": "000001.SZ",
                        "rank": 1,
                        "score": 80,
                        "decision": "buy",
                        "bull_case": "资金改善",
                        "bear_case": "市场波动",
                        "rebuttal": "设置止损",
                        "risk_control": "跌破支撑离场",
                    }
                ]
            }

    class PiClient:
        def start_judgment(self, got_request):
            calls.append(("start", got_request))
            assert got_request.mode == "single"
            assert got_request.limits.model_dump() == {"coarse": 1, "deep": 1, "final": 1}
            return "pi-run-1"

        def stream_events(self, run_id):
            calls.append(("events", run_id))
            return iter(())

        def get_result(self, run_id, got_request):
            calls.append(("result", run_id, got_request))
            return Result()

    calls = []
    manager.tracker = Tracker()
    manager._pi_agent_client = PiClient()
    manager._configs = lambda: (
        SimpleNamespace(provider="test", model="fake", reasoning_effort="low", max_tokens=100),
        SimpleNamespace(provider="test", model="fake"),
    )
    manager.freeze_agent_input = lambda candidates_n, ts_codes, as_of: frozen
    manager._persist_pi = lambda *args: calls.append(("persist", args))
    manager._publish_event = lambda *args, **kwargs: calls.append(("event", args, kwargs))
    monkeypatch.setattr(agents_service, "Store", Store)
    # 旧引擎的 run_single / run_judge / run_public_debate 已从本模块的导入里删掉,
    # 连符号都不存在——这比"打桩后断言没被调用"更强的保证:想调也调不到。
    for name in ("run_single", "run_judge", "run_public_debate"):
        assert not hasattr(agents_service, name), f"旧引擎符号 {name} 又被导入回来了"

    manager._run_single("task-1", "20260813", "000001.SZ")

    assert [call[0] for call in calls if call[0] in {"start", "result", "persist"}] == ["start", "result", "persist"]
    assert not any(call[0] == "finish" and call[2].get("status") == "failed" for call in calls)


def test_failure_events_refresh_the_heartbeat(monkeypatch):
    """只有 message.completed 刷心跳是不够的——断流那一轮几乎全是 message.failed。

    实测 20 只候选只产生 14 次 completed，心跳空档 19 分钟，超过启动回收的窗口，
    正在跑的任务会被判成僵死。收到任何事件都证明进程活着，都要刷。
    """
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager._pi_agent_status = {"availability": "available"}
    manager.set_pi_supervisor(None)
    manager.db_path = "unused.duckdb"
    request = _single_request()
    frozen = SimpleNamespace(
        candidates=request.candidates,
        snapshots=request.snapshots,
        candidate_hash=request.candidate_hash,
        input_hash=request.input_hash,
    )
    beats: list[str] = []

    class Tracker:
        def mark_running(self, task_id):
            pass

        def heartbeat(self, task_id):
            beats.append(task_id)

        def finish(self, task_id, **kwargs):
            pass

        def now(self):
            return "2026-08-13T00:00:00+00:00"

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def task_heartbeat(self, *_args, **_kwargs):
            pass

        def update_agent_run(self, *_args, **_kwargs):
            pass

        def upsert_agent_judgments(self, *_args, **_kwargs):
            pass

    class Result:
        def model_dump(self, **_kwargs):
            return {"final": []}

    class PiClient:
        def start_judgment(self, _request):
            return "pi-run-fail"

        def stream_events(self, _run_id):
            # 一只都没成功：全是失败事件，中间没有任何 completed。
            return iter([
                {"event_type": "message.failed", "ts_code": "000001.SZ", "error": "methodology stream aborted upstream"},
                {"event_type": "message.failed", "ts_code": "000002.SZ", "error": "bull stream aborted upstream"},
                {"event_type": "message.failed", "ts_code": "000003.SZ", "error": "trend stream aborted upstream"},
            ])

        def get_result(self, _run_id, _request):
            return Result()

    manager.tracker = Tracker()
    manager._pi_agent_client = PiClient()
    manager._configs = lambda: (
        SimpleNamespace(provider="test", model="fake", reasoning_effort="low", max_tokens=100),
        SimpleNamespace(provider="test", model="fake"),
    )
    manager.freeze_agent_input = lambda *args, **kwargs: frozen
    manager._relay_pi_event = lambda *args, **kwargs: None
    manager._publish_event = lambda *args, **kwargs: None
    manager._persist_pi = lambda *args: None
    monkeypatch.setattr(agents_service, "Store", Store)

    manager._run("task-hb", "20260813", 3, 3, 3, None)

    assert beats == ["task-hb"] * 3, f"失败事件没有刷心跳，实际刷了 {len(beats)} 次"

def test_relay_pi_event_writes_role_stage_round_into_columns(tmp_path):
    """Pi 的公开事件必须把 role/stage/round_no/ts_code 落到独立列。

    前端六格辩论面板是按 role 取最新一条消息渲染的，只要中继把 role 丢进
    content 里而不落列，面板永远是空的。
    """
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager.db_path = tmp_path / "relay.duckdb"
    manager.event_bus = agents_service.AgentEventBus(manager.db_path)

    # 形状与 app/services/pi_agent.py events() 的产出一致：Pi 的 data 字典被摊平，
    # 再注入 event_type 与 source_seq。
    manager._relay_pi_event("run-relay", {
        "event_type": "message.completed",
        "source_seq": 7,
        "role": "bull_counter",
        "stage": "debate",
        "round_no": 6,
        "ts_code": "000001.SZ",
        "summary": "逐条反驳空方",
        "citations": ["bear:transcript"],
    })

    with agents_service.Store(manager.db_path, ensure_schema=False) as store:
        rows = store.agent_events("run-relay")

    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "bull_counter"
    assert row["stage"] == "debate"
    assert row["round_no"] == 6
    assert row["ts_code"] == "000001.SZ"
    assert row["event_type"] == "message.completed"
    assert json.loads(row["content_json"])["summary"] == "逐条反驳空方"
    assert json.loads(row["citations_json"]) == ["bear:transcript"]


def test_relay_pi_event_keeps_run_level_events_without_role(tmp_path):
    """run.started 这类没有角色的事件照样入库，只是 role/round_no 为空。"""
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager.db_path = tmp_path / "relay-run.duckdb"
    manager.event_bus = agents_service.AgentEventBus(manager.db_path)

    manager._relay_pi_event("run-plain", {
        "event_type": "run.started",
        "source_seq": 1,
        "run_id": "pi-run-1",
        "input_hash": "a" * 64,
    })

    with agents_service.Store(manager.db_path, ensure_schema=False) as store:
        rows = store.agent_events("run-plain")

    assert len(rows) == 1
    assert rows[0]["event_type"] == "run.started"
    assert rows[0]["role"] == ""
    assert rows[0]["round_no"] is None
    assert rows[0]["ts_code"] is None
