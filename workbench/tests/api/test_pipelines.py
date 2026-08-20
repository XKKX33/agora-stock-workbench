"""盘后任务链 API:手动触发、幂等重跑、闸门拒绝与状态查询。

这里的固定装置(tests/api/conftest.py)灌的是 2025 年的合成行情,
交易日历也只覆盖到那一段。因此:

- 不带 trade_date 的自动触发一定被闸门以 `calendar_stale` 拒绝——
  这正是"日历没覆盖到今天就不猜"的期望行为,不是测试环境的缺陷。
- 完整离线闭环走手动指定 trade_date 的路径,它只校验目标日是开市日。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from engine.db import Store


from tests.test_run_scan_offline import AS_OF, _TRADE_DATES

# 固定装置的 160 个日历日全部开市:基准日 = AS_OF,可见日 = 基准日往前退 20 个
# 开市日。隐藏窗口内的日期(含 AS_OF)在提交阶段就该被可见性闸门拒绝。
VISIBLE_AS_OF = _TRADE_DATES[-21]
# 补齐 20 天的目标区间:以可见日结尾、由旧到新
BACKFILL_DATES = list(_TRADE_DATES[-40:-20])


ONE_CLICK_STEPS = [
    "preflight",
    "calendar",
    "market_data",
    "backfill_returns",
    "integrity",
    "scan",
    "collect_news",
    "agents",
    "persist_experiment",
]


@pytest.fixture(autouse=True)
def fake_one_click_runner(monkeypatch):
    """接口层只隔离联网和模型；任务落库、线程与轮询仍走真实实现。"""
    import app.services.pipelines as pipelines

    seen_trade_dates = []

    class FakeRunner:
        def __init__(self, _db_path):
            pass

        def run(
            self,
            *,
            run_id,
            strategy,
            trade_date,
            online,
            exchange,
            refresh_latest=True,
            collect_live_news=True,
            on_step=None,
            on_complete=None,
        ):
            seen_trade_dates.append(trade_date)
            if trade_date == "20250105":
                raise ValueError("更新日历后确认该日期不是交易日")
            # 真实执行器只写可见窗口内的日期;自动日期(None)落到可见日
            as_of = trade_date or VISIBLE_AS_OF
            steps = []
            group_counts = {}
            for name in ONE_CLICK_STEPS:
                if name == "persist_experiment":
                    group_counts = {
                        "rule": 3,
                        "ai": 3,
                        "hybrid": 3,
                        "benchmark": 20,
                    }
                steps.append({"name": name, "status": "ok", "data": {}})
                if on_step is not None:
                    on_step(
                        {
                            "current_step": name,
                            "steps": list(steps),
                            "as_of": as_of,
                            "group_counts": group_counts,
                        }
                    )
            return {
                "run_id": run_id,
                "as_of": as_of,
                "strategy": strategy,
                "online": online,
                "current_step": ONE_CLICK_STEPS[-1],
                "steps": steps,
                "group_counts": group_counts,
                "data_cutoff_at": "2025-01-03T15:30:00+08:00",
            }

    monkeypatch.setattr(pipelines, "OneClickRunner", FakeRunner)
    return seen_trade_dates


def _post_pipeline(client, **overrides) -> object:
    body: dict = {"online": False}
    body.update(overrides)
    return client.post("/api/pipelines", json=body)


def _wait_for_pipeline(client, job_id: str, timeout: float = 60.0) -> dict:
    """等待链条跑完。超时直接失败,不返回中间态让断言误判成功。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/pipelines/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"盘后任务链超时: {job_id}")


# ---------------------------------------------------------------- 完整离线闭环


@pytest.mark.api
def test_manual_pipeline_runs_full_offline_chain(client):
    """一键任务的九个步骤、四组数量和截止时间都要持久化。"""
    response = _post_pipeline(client, trade_date=VISIBLE_AS_OF)

    assert response.status_code == 202
    assert response.json()["reused"] is False
    job_id = response.json()["job_id"]
    payload = _wait_for_pipeline(client, job_id)

    assert payload["status"] == "succeeded", payload.get("error")
    result = payload["result"]
    assert [step["name"] for step in result["steps"]] == ONE_CLICK_STEPS
    assert result["current_step"] == "persist_experiment"
    assert result["as_of"] == VISIBLE_AS_OF
    assert result["group_counts"] == {
        "rule": 3,
        "ai": 3,
        "hybrid": 3,
        "benchmark": 20,
    }
    assert result["data_cutoff_at"]
    assert result["run_id"] == job_id


@pytest.mark.api
# ---------------------------------------------------------------- 幂等


@pytest.mark.api
def test_repeat_pipeline_reuses_completed_batch(client):
    """同一交易日同策略重复触发:不重复写入,返回既有批次。

    用 200 而不是 202:根本没有新任务被排队,202 会让前端一直轮询
    一个不会再变的任务。
    """
    first = _post_pipeline(client, trade_date=VISIBLE_AS_OF)
    assert first.status_code == 202
    first_id = first.json()["job_id"]
    _wait_for_pipeline(client, first_id)

    second = _post_pipeline(client, trade_date=VISIBLE_AS_OF)

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["job_id"] == first_id


@pytest.mark.api
def test_repeat_auto_pipeline_reuses_completed_signal_date(
    client, fake_one_click_runner
):
    """自动日期完成后必须按真实信号日复用，不能按墙钟日期重复建批次。"""
    first = _post_pipeline(client)
    assert first.status_code == 202
    first_id = first.json()["job_id"]
    _wait_for_pipeline(client, first_id)

    second = _post_pipeline(client)
    if second.status_code == 202:
        _wait_for_pipeline(client, second.json()["job_id"])

    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["job_id"] == first_id
    assert fake_one_click_runner == [None]


@pytest.mark.api
def test_online_auto_pipeline_enqueues_calendar_discovery_before_reuse(db_path):
    from app.services.pipelines import PipelineManager, TASK_KIND

    manager = PipelineManager(db_path)
    config = manager.config()
    old = manager.tracker.claim(
        kind=TASK_KIND,
        trade_date=AS_OF,
        strategy=config.strategy,
    )
    manager.tracker.mark_running(old.task_id)
    manager.tracker.finish(old.task_id, status="succeeded", result={"as_of": AS_OF})

    submitted = []

    class CapturingExecutor:
        def submit(self, function, *args):
            submitted.append((function, args))

        def shutdown(self, **_kwargs):
            pass

    manager._executor = CapturingExecutor()
    response = manager.start(
        online=True,
        now=datetime(2025, 1, 6, 16, 0),
    )

    assert response["reused"] is False
    assert response["trade_date"] == VISIBLE_AS_OF
    assert submitted and submitted[0][1][1] is None


@pytest.mark.api
def test_online_submission_claims_task_before_contacting_market(
    db_path, monkeypatch
):
    """数据源故障也必须先留下任务，真实日期只在 calendar 步骤确认。"""
    from app.services.pipelines import PipelineManager

    manager = PipelineManager(db_path)

    def forbidden_market_probe():
        raise RuntimeError("提交阶段不应访问市场数据源")

    monkeypatch.setattr(
        manager,
        "_latest_online_trade_date",
        forbidden_market_probe,
        raising=False,
    )
    submitted = []

    class CapturingExecutor:
        def submit(self, function, *args):
            submitted.append((function, args))

        def shutdown(self, **_kwargs):
            pass

    manager._executor = CapturingExecutor()
    response = manager.start(online=True, now=datetime(2026, 8, 5, 16, 0))

    assert response["status"] == "queued"
    assert manager.tracker.get(response["task_id"])["status"] == "queued"
    assert submitted and submitted[0][1][1] is None


@pytest.mark.api
def test_executor_rejection_marks_claimed_pipeline_failed(db_path):
    from app.services.pipelines import PipelineManager

    manager = PipelineManager(db_path)

    class RejectingExecutor:
        def submit(self, _function, *_args):
            raise RuntimeError("线程池已关闭")

        def shutdown(self, **_kwargs):
            pass

    manager._executor = RejectingExecutor()

    with pytest.raises(RuntimeError, match="线程池已关闭"):
        manager.start(
            online=True,
            now=datetime(2026, 8, 5, 16, 0),
        )

    latest = manager.tracker.latest(kind="one_click_pipeline")
    assert latest is not None
    assert latest["status"] == "failed"
    assert latest["error"]["failed_step"] == "preflight"


@pytest.mark.api
def test_initial_running_transition_failure_closes_queued_pipeline(
    db_path, monkeypatch
):
    from app.services.pipelines import PipelineManager, TASK_KIND

    manager = PipelineManager(db_path)
    claim = manager.tracker.claim(
        kind=TASK_KIND,
        trade_date=AS_OF,
        strategy=manager.config().strategy,
    )
    monkeypatch.setattr(
        manager.tracker,
        "mark_running",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("首次状态写入失败")),
    )

    with pytest.raises(RuntimeError, match="首次状态写入失败"):
        manager._run(claim.task_id, AS_OF, manager.config().strategy, False)

    task = manager.tracker.get(claim.task_id)
    assert task is not None
    assert task["status"] == "failed"
    assert task["error"]["failed_step"] == "preflight"


@pytest.mark.api
def test_atomic_completion_does_not_reread_or_downgrade_success(
    db_path, monkeypatch
):
    import app.services.pipelines as pipelines
    from tests.test_experiment_store import (
        _decisions,
        _run_row,
        _scan_completion,
    )

    manager = pipelines.PipelineManager(db_path)
    claim = manager.tracker.claim(
        kind=pipelines.TASK_KIND,
        trade_date=AS_OF,
        strategy="hermes",
    )

    class CompletingRunner:
        def __init__(self, _db_path):
            pass

        def run(self, *, run_id, on_complete, **_kwargs):
            result = {
                "run_id": run_id,
                "as_of": AS_OF,
                "steps": [],
                "group_counts": {},
            }
            on_complete(result, (_run_row(run_id), _decisions(run_id), object()))
            return result

    monkeypatch.setattr(pipelines, "OneClickRunner", CompletingRunner)
    monkeypatch.setattr(
        pipelines,
        "scan_completion_payload",
        lambda _result: _scan_completion(claim.task_id),
    )

    def forbidden_read(_task_id):
        raise RuntimeError("原子成功后不应再读取任务")

    monkeypatch.setattr(manager.tracker, "get", forbidden_read)

    manager._run(claim.task_id, AS_OF, "hermes", False)

    with pipelines.Store(db_path, ensure_schema=False) as store:
        assert store.get_task(claim.task_id)["status"] == "succeeded"


@pytest.mark.api
def test_one_click_does_not_reuse_legacy_close_pipeline(client, db_path):
    """旧五步成功记录不能冒充新九步一键流程。"""
    from app.services.tasks import TaskTracker

    tracker = TaskTracker(db_path)
    legacy = tracker.claim(
        kind="close_pipeline",
        trade_date=VISIBLE_AS_OF,
        strategy="strong_mainup",
    )
    assert legacy.claimed is True
    tracker.mark_running(legacy.task_id)
    tracker.finish(
        legacy.task_id,
        status="succeeded",
        result={"steps": [{"name": "scan", "status": "ok"}]},
    )

    response = _post_pipeline(
        client,
        trade_date=VISIBLE_AS_OF,
        strategy="strong_mainup",
    )

    assert response.status_code == 202
    assert response.json()["reused"] is False
    assert response.json()["kind"] == "one_click_pipeline"
    assert response.json()["job_id"] != legacy.task_id
    assert _wait_for_pipeline(client, response.json()["job_id"])["status"] == "succeeded"


@pytest.mark.api
def test_force_rerun_creates_new_pipeline(client):
    first_id = _post_pipeline(client, trade_date=VISIBLE_AS_OF).json()["job_id"]
    _wait_for_pipeline(client, first_id)

    second = _post_pipeline(client, trade_date=VISIBLE_AS_OF, force=True)

    assert second.status_code == 202
    assert second.json()["job_id"] != first_id
    assert _wait_for_pipeline(client, second.json()["job_id"])["status"] == "succeeded"


@pytest.mark.api
def test_pipeline_trade_date_matches_written_batch(client):
    """任务的 trade_date 必须等于扫描实际写入的 as_of。

    抢占时用的是闸门预解析的日期;在线模式可能抓到更新的交易日。
    不回写真实 as_of,幂等键就会指向一个并不存在的批次,重跑拦不住。
    """
    job_id = _post_pipeline(client, trade_date=VISIBLE_AS_OF).json()["job_id"]
    payload = _wait_for_pipeline(client, job_id)

    assert payload["trade_date"] == payload["result"]["as_of"]


# ---------------------------------------------------------------- 交易日校验


@pytest.mark.api
def test_non_trading_day_is_rejected(client):
    """先更新日历再判定非交易日；失败必须留在 task_runs。"""
    response = _post_pipeline(client, trade_date="20250105")  # 周日

    assert response.status_code == 202
    payload = _wait_for_pipeline(client, response.json()["job_id"])
    assert payload["status"] == "failed"
    assert payload["error"]["failed_step"] == "preflight"


@pytest.mark.api
def test_malformed_trade_date_is_rejected(client):
    response = _post_pipeline(client, trade_date="2025-13-99")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_trade_date"


@pytest.mark.api
def test_auto_trigger_is_not_blocked_by_stale_calendar(
    client, fake_one_click_runner
):
    """旧日历不能在一键任务的 calendar 更新步骤之前阻塞提交。"""
    response = _post_pipeline(client)

    assert response.status_code == 202
    payload = _wait_for_pipeline(client, response.json()["job_id"])
    assert payload["status"] == "succeeded"
    assert payload["result"]["steps"][1]["name"] == "calendar"
    assert fake_one_click_runner == [None]


# ---------------------------------------------------------------- 状态查询


@pytest.mark.api
def test_schedule_status_reports_config_and_gate(client):
    response = client.get("/api/pipelines/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_after"]
    assert payload["strategy"]
    assert payload["gate"]["should_run"] is False
    assert payload["gate"]["reason"] == "calendar_stale"
    # enabled 与 running 必须分开上报:关掉配置和线程崩了是两回事
    assert "enabled" in payload and "running" in payload


@pytest.mark.api
def test_status_route_not_shadowed_by_job_id(client):
    """/pipelines/status 不能被 /pipelines/{job_id} 吃掉。"""
    assert client.get("/api/pipelines/status").status_code == 200
    assert client.get("/api/pipelines/not-a-real-job").status_code == 404


@pytest.mark.api
def test_unknown_pipeline_returns_not_found(client):
    response = client.get("/api/pipelines/not-found")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "pipeline_job_not_found"


@pytest.mark.api
def test_list_recent_pipelines(client):
    job_id = _post_pipeline(client, trade_date=VISIBLE_AS_OF).json()["job_id"]
    _wait_for_pipeline(client, job_id)

    response = client.get("/api/pipelines", params={"limit": 5})

    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert items[0]["job_id"] == job_id
    assert items[0]["kind"] == "one_click_pipeline"


@pytest.mark.api
def test_invalid_limit_rejected(client):
    response = client.get("/api/pipelines", params={"limit": 0})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_limit"

@pytest.mark.api
def test_pipeline_workflow_definition_exposes_contract(client):
    response = client.get("/api/pipelines/workflow")
    assert response.status_code == 200
    definition = response.json()

    assert [item["name"] for item in definition["steps"]] == ONE_CLICK_STEPS
    assert "strategy" in definition
    assert "online" in definition
    assert "data_cutoff_at" in definition

@pytest.mark.api
def test_agent_failure_marks_task_failed_without_succeeded_experiment(
    client, db_path, monkeypatch
):
    import app.services.pipelines as pipelines
    from tests.test_one_click import _run_row

    class AgentFailRunner:
        def __init__(self, _db_path):
            pass

        def run(self, *, run_id, on_step, **_kwargs):
            steps = []
            for name in ONE_CLICK_STEPS[:7]:
                steps.append({"name": name, "status": "ok", "data": {}})
                on_step({"current_step": name, "steps": list(steps), "as_of": AS_OF})
            with Store(db_path, ensure_schema=True) as store:
                store.create_experiment_run(_run_row(run_id))
            raise RuntimeError("agent stage failed")

    monkeypatch.setattr(pipelines, "OneClickRunner", AgentFailRunner)
    response = _post_pipeline(client, trade_date=VISIBLE_AS_OF)
    payload = _wait_for_pipeline(client, response.json()["job_id"])

    assert payload["status"] == "failed"
    assert payload["error"]["failed_step"] == "agents"
    with Store(db_path, ensure_schema=False) as store:
        row = store.experiment_run(payload["job_id"])
    assert row is not None
    assert row["status"] != "succeeded"



# ------------------------------------------ 可见日期闸门与补齐最近可见交易日


def _post_backfill(client, **overrides) -> object:
    body: dict = {"online": False}
    body.update(overrides)
    return client.post("/api/pipelines/backfill", json=body)


@pytest.mark.api
def test_hidden_window_trade_date_is_rejected(client):
    """基准日落在隐藏窗口内:提交阶段就 400,不静默改写成可见日。"""
    response = _post_pipeline(client, trade_date=AS_OF)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "lookahead_blocked"


@pytest.mark.api
def test_auto_pipeline_claims_visible_session(client):
    """不指定日期时抢占键必须是可见日——它才是后台真实写入的 as_of。"""
    response = _post_pipeline(client)

    assert response.status_code == 202
    assert response.json()["trade_date"] == VISIBLE_AS_OF
    payload = _wait_for_pipeline(client, response.json()["job_id"])
    assert payload["status"] == "succeeded", payload.get("error")
    assert payload["result"]["as_of"] == VISIBLE_AS_OF


@pytest.mark.api
def test_backfill_targets_recent_visible_sessions(db_path):
    """补齐日期:20 个开市日、升序、末位=可见日,一天都不越进隐藏窗口。"""
    from app.services.pipelines import PipelineManager, TASK_KIND_BACKFILL

    manager = PipelineManager(db_path)
    submitted = []

    class CapturingExecutor:
        def submit(self, function, *args):
            submitted.append((function, args))

        def shutdown(self, **_kwargs):
            pass

    manager._executor = CapturingExecutor()
    job = manager.backfill(count=20, online=False)

    assert job["kind"] == TASK_KIND_BACKFILL
    assert job["status"] == "queued"
    assert job["reused"] is False
    assert job["count"] == 20
    assert job["dates"] == BACKFILL_DATES
    assert job["dates"] == sorted(job["dates"])
    assert job["dates"][-1] == VISIBLE_AS_OF
    assert job["trade_date"] == VISIBLE_AS_OF
    assert submitted and submitted[0][1][1] == BACKFILL_DATES


@pytest.mark.api
def test_backfill_endpoint_runs_visible_sessions_serially(client):
    """接口全链路:202 + 日期契约,并把每一天都跑成独立的一键任务。"""
    response = _post_backfill(client, count=3)

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "one_click_backfill"
    assert body["count"] == 3
    assert body["dates"] == BACKFILL_DATES[-3:]
    assert body["trade_date"] == VISIBLE_AS_OF

    payload = _wait_for_pipeline(client, body["job_id"])
    assert payload["status"] == "succeeded", payload.get("error")
    assert payload["result"]["completed"] == body["dates"]
    assert payload["result"]["remaining"] == []

    listing = client.get("/api/pipelines", params={"limit": 10})
    assert listing.status_code == 200
    assert {item["trade_date"] for item in listing.json()["items"]} >= set(
        body["dates"]
    )
    # kind 透传:补齐协调器只在显式指定 kind 时出现,默认列表仍只看一键链条
    coordinator = client.get("/api/pipelines", params={"kind": "one_click_backfill"})
    assert [item["job_id"] for item in coordinator.json()["items"]] == [body["job_id"]]


@pytest.mark.api
def test_repeat_backfill_reuses_completed_batch(client):
    """同一批补齐已成功:返回既有任务 + 200,不重排一遍。"""
    first = _post_backfill(client, count=1)
    assert first.status_code == 202
    first_id = first.json()["job_id"]
    assert _wait_for_pipeline(client, first_id)["status"] == "succeeded"

    second = _post_backfill(client, count=1)

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["job_id"] == first_id
    assert body["dates"] == [VISIBLE_AS_OF]
    assert body["count"] == 1


@pytest.mark.api
def test_backfill_reuses_succeeded_days_and_continues_after_failure(
    db_path, monkeypatch
):
    """串行补齐记录失败日期，但继续处理后续日期。"""
    from app.services.pipelines import PipelineManager, TASK_KIND, TASK_KIND_BACKFILL

    manager = PipelineManager(db_path)
    strategy = manager.config().strategy
    dates = BACKFILL_DATES[:4]

    done = manager.tracker.claim(
        kind=TASK_KIND, trade_date=dates[1], strategy=strategy
    )
    manager.tracker.mark_running(done.task_id)
    manager.tracker.finish(
        done.task_id, status="succeeded", result={"as_of": dates[1]}
    )

    calls = []

    def fake_run(
        task_id,
        trade_date,
        run_strategy,
        online,
        *,
        refresh_latest,
        collect_live_news,
    ):
        calls.append((trade_date, refresh_latest, collect_live_news))
        assert run_strategy == strategy
        if trade_date == dates[2]:
            raise RuntimeError("第三天摄取失败")
        manager.tracker.mark_running(task_id)
        manager.tracker.finish(
            task_id, status="succeeded", result={"as_of": trade_date}
        )

    monkeypatch.setattr(manager, "_run", fake_run)
    coordinator = manager.tracker.claim(
        kind=TASK_KIND_BACKFILL, trade_date=dates[-1], strategy=strategy
    )

    manager._run_backfill(coordinator.task_id, dates, strategy, False, False)

    assert [item[0] for item in calls] == [dates[0], dates[2], dates[3]]
    # refresh_latest 只在列表第一天为 True;补历史一律不采集当天热榜
    assert [item[1] for item in calls] == [True, False, False]
    assert [item[2] for item in calls] == [False, False, False]

    task = manager.tracker.get(coordinator.task_id)
    assert task["status"] == "succeeded"
    assert task["result"]["has_warnings"] is True
    assert task["result"]["failed"][0]["date"] == dates[2]
    assert task["result"]["completed"] == [dates[0], dates[3]]
    assert task["result"]["reused"] == [dates[1]]
    assert task["result"]["remaining"] == []


@pytest.mark.api
def test_backfill_skips_a_session_already_running_and_continues(db_path, monkeypatch):
    """某天已有活跃任务时记录警告，继续补后续日期。"""
    from app.services.pipelines import PipelineManager, TASK_KIND, TASK_KIND_BACKFILL

    manager = PipelineManager(db_path)
    strategy = manager.config().strategy
    dates = BACKFILL_DATES[:3]

    busy = manager.tracker.claim(
        kind=TASK_KIND, trade_date=dates[1], strategy=strategy
    )
    manager.tracker.mark_running(busy.task_id)

    calls = []

    def fake_run(task_id, trade_date, *_args, **_kwargs):
        calls.append(trade_date)
        manager.tracker.mark_running(task_id)
        manager.tracker.finish(
            task_id, status="succeeded", result={"as_of": trade_date}
        )

    monkeypatch.setattr(manager, "_run", fake_run)
    coordinator = manager.tracker.claim(
        kind=TASK_KIND_BACKFILL, trade_date=dates[-1], strategy=strategy
    )

    manager._run_backfill(coordinator.task_id, dates, strategy, False, False)

    assert calls == [dates[0], dates[2]]
    task = manager.tracker.get(coordinator.task_id)
    assert task["status"] == "succeeded"
    assert task["result"]["has_warnings"] is True
    assert task["result"]["failed"][0]["date"] == dates[1]
    assert task["result"]["remaining"] == []


@pytest.mark.api
def test_backfill_count_out_of_range_is_rejected(db_path, client):
    """两层刹车:manager 层 400 invalid_backfill_count,schema 层 422。"""
    from app.errors import WorkbenchError
    from app.services.pipelines import PipelineManager

    manager = PipelineManager(db_path)
    for bad in (0, 121):
        with pytest.raises(WorkbenchError) as excinfo:
            manager.backfill(count=bad)
        assert excinfo.value.code == "invalid_backfill_count"
        assert excinfo.value.status_code == 400

    response = _post_backfill(client, count=0)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


@pytest.mark.api
def test_backfill_route_not_shadowed_by_job_id(client):
    """POST /pipelines/backfill 必须真的生效,不能被 /pipelines/{job_id} 吃掉。"""
    response = _post_backfill(client, count=1)

    assert response.status_code == 202
    assert response.json()["dates"] == [VISIBLE_AS_OF]
    # GET 同名路径仍走 {job_id},这正是必须把 POST 声明在前面的原因
    assert client.get("/api/pipelines/backfill").status_code == 404
    assert _wait_for_pipeline(client, response.json()["job_id"])["status"] == "succeeded"
