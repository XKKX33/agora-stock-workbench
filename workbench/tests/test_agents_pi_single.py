from __future__ import annotations

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


def test_run_single_uses_pi_client_and_never_old_engine(monkeypatch):
    manager = AgentJudgeManager.__new__(AgentJudgeManager)
    manager._pi_agent_status = {"availability": "available"}
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
    monkeypatch.setattr(agents_service, "run_single", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old engine called")))

    manager._run_single("task-1", "20260813", "000001.SZ")

    assert [call[0] for call in calls if call[0] in {"start", "result", "persist"}] == ["start", "result", "persist"]
    assert not any(call[0] == "finish" and call[2].get("status") == "failed" for call in calls)
