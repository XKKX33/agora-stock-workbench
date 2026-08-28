from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.db import Store

pytestmark = pytest.mark.integration


def _run_row(run_id: str, *, created_at: str, heartbeat_at: str | None = None) -> dict:
    return {
        "run_id": run_id,
        "as_of": "20260821",
        "data_cutoff_at": f"{created_at}",
        "status": "running",
        "strategy_name": "strong_mainup",
        "strategy_version": "1",
        "model": "minimax-m3",
        "temperature": 0.0,
        "prompt_version": "1",
        "candidate_hash": "h" * 16,
        "candidate_count": 20,
        "final_count": 3,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": created_at,
        "finished_at": None,
        "error_json": None,
    }


class TestStaleRunReclaim:
    """进程被强杀后，running 批次必须被收尾。

    `running` 在库里无法区分「正在跑」和「跑它的进程早就死了」。收尾逻辑写在
    流程函数尾部，进程一死就不执行，批次于是永久停在 running——后续看板、
    统计、幂等判断全都会把它当成活跃任务。启动时必须回收。
    """

    def test_stale_running_run_is_marked_interrupted(self, tmp_path: Path):
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.create_experiment_run(
                _run_row("stale-run", created_at="2026-08-21T10:00:00+00:00")
            )

            reclaimed = store.reclaim_stale_experiment_runs(
                now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
            )

            assert reclaimed == ["stale-run"]
            row = store.experiment_run("stale-run")
            assert row["status"] == "failed"
            assert row["finished_at"] == "2026-08-21T12:00:00+00:00"
            assert "进程中断" in row["error_json"]

    def test_fresh_running_run_is_left_alone(self, tmp_path: Path):
        """还在心跳窗口内的批次不许动——那可能是另一个正在跑的进程。"""
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.create_experiment_run(
                _run_row("fresh-run", created_at="2026-08-21T11:59:00+00:00")
            )

            reclaimed = store.reclaim_stale_experiment_runs(
                now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
            )

            assert reclaimed == []
            assert store.experiment_run("fresh-run")["status"] == "running"

    def test_succeeded_run_is_never_downgraded(self, tmp_path: Path):
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            row = _run_row("done-run", created_at="2026-08-21T10:00:00+00:00")
            store.create_experiment_run(row)
            store.con.execute(
                "UPDATE experiment_runs SET status = 'succeeded' WHERE run_id = 'done-run'"
            )

            assert (
                store.reclaim_stale_experiment_runs(
                    now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
                )
                == []
            )
            assert store.experiment_run("done-run")["status"] == "succeeded"

    def test_agent_run_heartbeat_extends_the_window(self, tmp_path: Path):
        """agent_runs 有心跳列，就按心跳算：长流程只要还在报活就不该被判死。"""
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.con.execute(
                """
                INSERT INTO agent_runs (run_id, as_of, status, stage, created_at, heartbeat_at)
                VALUES ('beating-agent', '20260821', 'running', 'deep',
                        '2026-08-21T10:00:00+00:00', '2026-08-21T11:59:30+00:00')
                """
            )

            assert (
                store.reclaim_stale_agent_runs(
                    now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
                )
                == []
            )
            status = store.con.execute(
                "SELECT status FROM agent_runs WHERE run_id = 'beating-agent'"
            ).fetchone()[0]
            assert status == "running"

    def test_stale_agent_run_is_reclaimed_too(self, tmp_path: Path):
        """agent_runs 有同样的洞：它也只在进程活着时才会被收尾。"""
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.con.execute(
                """
                INSERT INTO agent_runs (run_id, as_of, status, stage, created_at, heartbeat_at)
                VALUES ('stale-agent', '20260821', 'running', 'deep',
                        '2026-08-21T10:00:00+00:00', '2026-08-21T10:00:00+00:00')
                """
            )

            reclaimed = store.reclaim_stale_agent_runs(
                now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
            )

            assert reclaimed == ["stale-agent"]
            status = store.con.execute(
                "SELECT status FROM agent_runs WHERE run_id = 'stale-agent'"
            ).fetchone()[0]
            assert status == "failed"

    def test_stale_task_run_is_reclaimed_too(self, tmp_path: Path):
        """task_runs 是第三个洞：任务中心和 /api/pipelines/{id} 都读它。

        claim_task 只在抢占时把僵死行绕开，行本身仍是 running，于是看板永远显示
        「正在跑」，轮询也永远等不到终态——实测被它骗了 40 分钟。
        """
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.con.execute(
                """
                INSERT INTO task_runs (task_id, kind, trade_date, strategy, status,
                                       created_at, started_at, heartbeat_at)
                VALUES ('stale-task', 'one_click_pipeline', '20260821', 'strong_mainup',
                        'running', '2026-08-21T10:00:00+00:00',
                        '2026-08-21T10:00:00+00:00', '2026-08-21T10:00:00+00:00')
                """
            )

            reclaimed = store.reclaim_stale_task_runs(
                now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
            )

            assert reclaimed == ["stale-task"]
            row = store.con.execute(
                "SELECT status, error_json FROM task_runs WHERE task_id = 'stale-task'"
            ).fetchone()
            assert row[0] == "failed"
            assert "进程中断" in row[1]

    def test_task_run_still_reporting_is_left_alone(self, tmp_path: Path):
        """还在报心跳的任务不许动，否则会打断另一个进程正在跑的流程。"""
        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.con.execute(
                """
                INSERT INTO task_runs (task_id, kind, trade_date, strategy, status,
                                       created_at, started_at, heartbeat_at)
                VALUES ('busy-task', 'agent_judge', '20260821', 'strong_mainup',
                        'running', '2026-08-21T08:00:00+00:00',
                        '2026-08-21T08:00:00+00:00', '2026-08-21T11:59:00+00:00')
                """
            )

            reclaimed = store.reclaim_stale_task_runs(
                now="2026-08-21T12:00:00+00:00", max_idle_seconds=600
            )

            assert reclaimed == []
            status = store.con.execute(
                "SELECT status FROM task_runs WHERE task_id = 'busy-task'"
            ).fetchone()[0]
            assert status == "running"


class TestStartupReclaim:
    """光有回收方法不算修好，服务启动必须真的调用它。"""

    def test_service_startup_reclaims_stale_runs(self, tmp_path: Path):
        from fastapi.testclient import TestClient

        from app.config import AppSettings
        from app.main import create_app

        db_path = tmp_path / "market.duckdb"
        with Store(db_path, ensure_schema=True) as store:
            store.create_experiment_run(
                _run_row("killed-run", created_at="2020-01-01T00:00:00+00:00")
            )
            store.con.execute(
                """
                INSERT INTO agent_runs (run_id, as_of, status, stage, created_at, heartbeat_at)
                VALUES ('killed-agent', '20260821', 'running', 'deep',
                        '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
                """
            )
            store.con.execute(
                """
                INSERT INTO task_runs (task_id, kind, trade_date, strategy, status,
                                       created_at, started_at, heartbeat_at)
                VALUES ('killed-task', 'one_click_pipeline', '20260821', 'strong_mainup',
                        'running', '2020-01-01T00:00:00+00:00',
                        '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
                """
            )

        settings = AppSettings(database_path=db_path)
        with TestClient(create_app(settings)):
            pass

        with Store(db_path, ensure_schema=False) as store:
            assert store.experiment_run("killed-run")["status"] == "failed"
            status = store.con.execute(
                "SELECT status FROM agent_runs WHERE run_id = 'killed-agent'"
            ).fetchone()[0]
            assert status == "failed"
            task_status = store.con.execute(
                "SELECT status FROM task_runs WHERE task_id = 'killed-task'"
            ).fetchone()[0]
            assert task_status == "failed", "任务中心的僵死行没被收尾"

    def test_startup_never_kills_a_run_another_process_is_still_working_on(
        self, tmp_path: Path
    ):
        """同目录起第二个实例时，第一个实例正在跑的批次必须活下来。

        原先启动用的窗口是 0，理由写的是"DuckDB 单写者，能打开库就说明没有并行写
        进程"。这个推理是错的：Store 打开后立刻关闭，不持有写锁。实测撞上了——第二
        个实例把第一个实例正在跑的研判收成了 failed，而它还在继续调模型。
        """
        from fastapi.testclient import TestClient

        from app.config import AppSettings
        from app.main import create_app

        db_path = tmp_path / "market.duckdb"
        fresh = datetime.now(timezone.utc).isoformat()
        with Store(db_path, ensure_schema=True) as store:
            store.create_experiment_run(_run_row("live-run", created_at=fresh))
            store.con.execute(
                """
                INSERT INTO agent_runs (run_id, as_of, status, stage, created_at, heartbeat_at)
                VALUES ('live-agent', '20260821', 'running', 'debate', ?, ?)
                """,
                [fresh, fresh],
            )
            store.con.execute(
                """
                INSERT INTO task_runs (task_id, kind, trade_date, strategy, status,
                                       created_at, started_at, heartbeat_at)
                VALUES ('live-task', 'one_click_pipeline', '20260821', 'strong_mainup',
                        'running', ?, ?, ?)
                """,
                [fresh, fresh, fresh],
            )

        settings = AppSettings(database_path=db_path)
        with TestClient(create_app(settings)):
            pass

        with Store(db_path, ensure_schema=False) as store:
            assert store.experiment_run("live-run")["status"] == "running", (
                "刚报活的实验批次被误收了"
            )
            status = store.con.execute(
                "SELECT status FROM agent_runs WHERE run_id = 'live-agent'"
            ).fetchone()[0]
            assert status == "running", "刚报活的 Agent 运行被误收了"
            task_status = store.con.execute(
                "SELECT status FROM task_runs WHERE task_id = 'live-task'"
            ).fetchone()[0]
            assert task_status == "running", "刚报活的任务被误收了"
