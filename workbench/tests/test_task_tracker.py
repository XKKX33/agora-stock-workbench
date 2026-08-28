from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.services.tasks import TaskTracker
from engine.db import Store


def test_task_claims_table_has_business_key_primary_key(tmp_path):
    with Store(tmp_path / "claims.duckdb") as store:
        row = store.con.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'task_claims' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()

    assert row is not None
    assert row[0] == ["kind", "trade_date", "strategy_key"]


def test_independent_trackers_cannot_both_claim_same_business_key(tmp_path):
    db_path = tmp_path / "concurrent-claims.duckdb"
    with Store(db_path):
        pass
    barrier = Barrier(2)

    def claim_once():
        tracker = TaskTracker(db_path)
        barrier.wait()
        return tracker.claim(
            kind="one_click_pipeline",
            trade_date="20260804",
            strategy="hermes",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim_once(), range(2)))

    assert sum(result.claimed for result in results) == 1
    loser = next(result for result in results if not result.claimed)
    assert loser.conflict is not None
    assert loser.conflict["status"] == "queued"
    with Store(db_path, ensure_schema=False) as store:
        assert store.con.execute(
            "SELECT COUNT(*) FROM task_runs "
            "WHERE kind='one_click_pipeline' AND trade_date='20260804' "
            "AND strategy='hermes' AND status='queued'"
        ).fetchone()[0] == 1


def test_terminal_task_status_cannot_be_downgraded(tmp_path):
    db_path = tmp_path / "terminal-status.duckdb"
    tracker = TaskTracker(db_path)
    claim = tracker.claim(
        kind="one_click_pipeline",
        trade_date="20260804",
        strategy="hermes",
    )
    tracker.mark_running(claim.task_id)
    tracker.finish(claim.task_id, status="succeeded", result={"ok": True})

    with pytest.raises(ValueError, match="终态"):
        tracker.finish(
            claim.task_id,
            status="failed",
            error={"message": "迟到的异常"},
        )

    assert tracker.get(claim.task_id)["status"] == "succeeded"


def test_terminal_task_cannot_be_reopened_as_running(tmp_path):
    db_path = tmp_path / "terminal-running.duckdb"
    tracker = TaskTracker(db_path)
    claim = tracker.claim(
        kind="one_click_pipeline",
        trade_date="20260804",
        strategy="hermes",
    )
    tracker.mark_running(claim.task_id)
    tracker.finish(claim.task_id, status="succeeded", result={"ok": True})

    with pytest.raises(ValueError, match="不可改为运行中"):
        tracker.mark_running(claim.task_id)

    assert tracker.get(claim.task_id)["status"] == "succeeded"


def test_stale_task_takeover_closes_running_experiment(tmp_path):
    db_path = tmp_path / "stale-experiment.duckdb"
    tracker = TaskTracker(db_path)
    first = tracker.claim(
        kind="one_click_pipeline",
        trade_date="20260804",
        strategy="hermes",
    )
    tracker.mark_running(first.task_id)
    with Store(db_path, ensure_schema=False) as store:
        store.con.execute(
            """
            INSERT INTO experiment_runs (
                run_id, as_of, status, strategy_name, created_at
            ) VALUES (?, '20260804', 'running', 'hermes', ?)
            """,
            [first.task_id, "2000-01-01T00:00:00+00:00"],
        )
        store.con.execute(
            "UPDATE task_runs SET heartbeat_at = ? WHERE task_id = ?",
            ["2000-01-01T00:00:00+00:00", first.task_id],
        )

    second = tracker.claim(
        kind="one_click_pipeline",
        trade_date="20260804",
        strategy="hermes",
        stale_after_seconds=1,
    )

    assert second.claimed is True
    assert second.task_id != first.task_id
    with Store(db_path, ensure_schema=False) as store:
        assert store.get_task(first.task_id)["status"] == "failed"
        experiment = store.experiment_run(first.task_id)
        assert experiment is not None
        assert experiment["status"] == "failed"
        assert "StaleTask" in experiment["error_json"]
def test_progress_event_persists_current_stage_steps_and_logs(tmp_path):
    db_path = tmp_path / "progress.duckdb"
    with Store(db_path):
        pass
    tracker = TaskTracker(db_path)
    claim = tracker.claim(kind="scan", trade_date="20260817", strategy="strong_mainup")
    tracker.mark_running(claim.task_id)
    tracker.update_progress(
        claim.task_id,
        stage="candidate_pool",
        step=4,
        total=7,
        message="初步候选 259 只",
        detail="候选池构建完成",
    )

    task = tracker.get(claim.task_id)

    assert task["result"]["progress"]["stage"] == "candidate_pool"
    assert task["result"]["progress"]["percent"] == 57
    assert task["result"]["steps"][0]["status"] == "running"
    assert task["result"]["progress"]["logs"][0]["message"] == "初步候选 259 只"
def test_terminal_result_keeps_progress_history(tmp_path):
    db_path = tmp_path / "terminal-progress.duckdb"
    with Store(db_path):
        pass
    tracker = TaskTracker(db_path)
    claim = tracker.claim(kind="news_collect", trade_date="20260817", strategy="")
    tracker.mark_running(claim.task_id)
    tracker.update_progress(
        claim.task_id,
        stage="fetch_sources",
        step=4,
        total=7,
        message="已抓取 12 条原始新闻",
    )
    tracker.finish(claim.task_id, status="succeeded", result={"stored": 8})

    task = tracker.get(claim.task_id)

    assert task["result"]["stored"] == 8
    assert task["result"]["progress"]["logs"][0]["message"] == "已抓取 12 条原始新闻"
    assert task["result"]["steps"][0]["name"] == "fetch_sources"
def test_failed_terminal_task_keeps_failed_progress_step(tmp_path):
    db_path = tmp_path / "failed-progress.duckdb"
    with Store(db_path):
        pass
    tracker = TaskTracker(db_path)
    claim = tracker.claim(kind="scan", trade_date="20260817", strategy="strong_mainup")
    tracker.mark_running(claim.task_id)
    tracker.update_progress(claim.task_id, stage="score", step=5, total=5, message="评分失败前最后阶段")
    tracker.finish(claim.task_id, status="failed", error={"message": "评分失败"})

    task = tracker.get(claim.task_id)

    assert task["status"] == "failed"
    assert task["result"]["steps"][0]["status"] == "failed"
