"""调度器轮询逻辑的单元测试。

用假的 PipelineManager,不碰数据库也不起线程:这一层要验的是
"闸门说什么、start() 抛什么,调度器就该做什么",与真实数据无关。

重点锁两条容易退化的行为:

1. 已完成的批次不重复触发(reused / 409 都算正常跳过,不记故障)。
2. 触发失败不能弄死轮询线程,但必须把原因写进 last_error 暴露出来。
"""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import WorkbenchError  # noqa: E402
from app.services.scheduler import CloseScheduler  # noqa: E402
from engine.schedule import GateDecision, ScheduleConfig  # noqa: E402

TRADE_DATE = "20260731"


def _config(*, enabled: bool = True) -> ScheduleConfig:
    return ScheduleConfig(
        enabled=enabled,
        run_after=time(15, 30),
        exchange="SSE",
        strategy="strong_mainup",
        online=False,
        tick_seconds=60,
    )


def _ready() -> GateDecision:
    return GateDecision(
        should_run=True, trade_date=TRADE_DATE, reason="ready", detail="可执行"
    )


def _blocked(reason: str = "before_run_after") -> GateDecision:
    return GateDecision(
        should_run=False, trade_date=TRADE_DATE, reason=reason, detail="还没到运行时间"
    )


class FakeManager:
    """只实现调度器用到的三个方法。start 的行为由构造参数决定。"""

    def __init__(self, *, config=None, decision=None, start_result=None, start_error=None):
        self._config = config or _config()
        self._decision = decision or _ready()
        self._start_result = start_result
        self._start_error = start_error
        self.start_calls = 0

    def config(self) -> ScheduleConfig:
        return self._config

    def gate(self, *, now=None) -> GateDecision:
        return self._decision

    def start(self, *, now=None) -> dict:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error
        return self._start_result or {
            "job_id": "job-1",
            "trade_date": TRADE_DATE,
            "reused": False,
        }

    def status(self, *, now=None) -> dict:
        return {"enabled": self._config.enabled, "gate": self._decision.as_dict()}


@pytest.mark.unit
def test_tick_starts_pipeline_when_gate_ready():
    manager = FakeManager()
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "started"
    assert manager.start_calls == 1
    assert scheduler.status()["last_error"] is None


@pytest.mark.unit
def test_tick_skips_when_gate_blocks():
    """闸门拒绝时不触发,也不算故障——没到点不跑是正常状态。"""
    manager = FakeManager(decision=_blocked())
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "skip"
    assert outcome["reason"] == "before_run_after"
    assert manager.start_calls == 0
    assert scheduler.status()["last_error"] is None


@pytest.mark.unit
def test_disabled_schedule_does_not_trigger():
    manager = FakeManager(config=_config(enabled=False))
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "disabled"
    assert manager.start_calls == 0


@pytest.mark.unit
def test_disabled_schedule_does_not_start_thread():
    """enabled=false 时不起线程,但状态要能说明原因,不是静默什么都不做。"""
    scheduler = CloseScheduler(FakeManager(config=_config(enabled=False)))

    assert scheduler.start() is False
    assert scheduler.running is False
    assert "enabled=false" in scheduler.status()["last_tick_detail"]


@pytest.mark.unit
def test_reused_batch_is_not_counted_as_failure():
    """命中已完成批次 = 幂等生效,属于正常结果。"""
    manager = FakeManager(
        start_result={"job_id": "old", "trade_date": TRADE_DATE, "reused": True}
    )
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "reused"
    assert scheduler.status()["last_error"] is None


@pytest.mark.unit
@pytest.mark.parametrize("code", ["pipeline_in_progress", "pipeline_not_due"])
def test_benign_conflicts_are_skips_not_errors(code):
    """上一轮还在跑 / 刚好跨过闸门边界:跳过本轮,不记故障。

    链条耗时超过 tick 间隔时 409 必然出现,把它记成错误会让状态里
    永远挂着一个红灯,真正的故障反而看不出来。
    """
    manager = FakeManager(start_error=WorkbenchError(code, "冲突", status_code=409))
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "skip"
    assert scheduler.status()["last_error"] is None


@pytest.mark.unit
def test_real_failure_is_recorded_but_loop_survives():
    """真正的失败要写进 last_error 暴露,但不能让轮询线程死掉。"""
    manager = FakeManager(
        start_error=WorkbenchError("no_market_data", "库里没数据", status_code=503)
    )
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "error"
    assert "no_market_data" in scheduler.status()["last_error"]
    # 再跑一轮仍然可用,没有因为上一轮的异常而不可恢复
    assert scheduler.tick()["action"] == "error"


@pytest.mark.unit
def test_unexpected_exception_does_not_escape_tick():
    """非 WorkbenchError 的异常同样被记录,不上抛到轮询线程。"""
    manager = FakeManager(start_error=RuntimeError("数据库连接断了"))
    scheduler = CloseScheduler(manager)

    outcome = scheduler.tick()

    assert outcome["action"] == "error"
    assert "RuntimeError" in scheduler.status()["last_error"]


@pytest.mark.unit
def test_error_is_cleared_after_a_good_tick():
    """故障恢复后要清掉 last_error,否则页面永远显示一个早已过期的错误。"""
    manager = FakeManager(start_error=RuntimeError("临时故障"))
    scheduler = CloseScheduler(manager)
    scheduler.tick()
    assert scheduler.status()["last_error"] is not None

    manager._start_error = None
    scheduler.tick()

    assert scheduler.status()["last_error"] is None


@pytest.mark.unit
def test_status_does_not_deadlock():
    """status() 同时要线程状态和快照字段。锁不可重入,写错就会死锁在这里。"""
    scheduler = CloseScheduler(FakeManager())

    payload = scheduler.status()

    assert payload["running"] is False
    assert "last_tick_at" in payload
