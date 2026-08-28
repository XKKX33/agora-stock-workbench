"""实验批次存储测试：全部使用 tmp_path 临时 DuckDB。"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from engine.db import Store
from engine.db_experiments import (
    ENTRY_STATUSES,
    classify_entry_status,
    entry_status_predicate,
)
from engine.schema import _LEGACY_DECISION_COLUMNS


GROUPS = {"rule", "ai", "hybrid", "benchmark"}
RUN_COLUMNS = {
    "run_id",
    "as_of",
    "data_cutoff_at",
    "status",
    "strategy_name",
    "strategy_version",
    "model",
    "temperature",
    "prompt_version",
    "candidate_hash",
    "candidate_count",
    "final_count",
    "hybrid_rule_weight",
    "hybrid_ai_weight",
    "created_at",
    "finished_at",
    "error_json",
}
DECISION_COLUMNS = {
    "run_id",
    "group_name",
    "ts_code",
    "name",
    "industry",
    "rank",
    "rule_score",
    "ai_score",
    "hybrid_score",
    "reason_json",
    "risk_json",
}


def _run_row(
    run_id: str,
    *,
    status: str = "running",
    candidate_count: int = 1,
    final_count: int = 1,
    as_of: str = "20260804",
) -> dict:
    return {
        "run_id": run_id,
        "as_of": as_of,
        "data_cutoff_at": "2026-08-04T15:30:00+08:00",
        "status": status,
        "strategy_name": "hermes",
        "strategy_version": "v1",
        "model": "deepseekv4flash",
        "temperature": 0.1,
        "prompt_version": "p1",
        "candidate_hash": "sha256:abc",
        "candidate_count": candidate_count,
        "final_count": final_count,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": "2026-08-04T15:31:00+08:00",
        "finished_at": None,
        "error_json": None,
    }


def _decisions(run_id: str) -> pd.DataFrame:
    rows = []
    for index, group_name in enumerate(("rule", "ai", "hybrid", "benchmark"), 1):
        rows.append(
            {
                "run_id": run_id,
                "group_name": group_name,
                "ts_code": f"00000{index}.SZ",
                "name": f"样本{index}",
                "industry": "测试行业",
                "rank": 1,
                "rule_score": 90.0 - index,
                "ai_score": 80.0 - index,
                "hybrid_score": 85.0 - index,
                "reason_json": json.dumps({"group": group_name}, ensure_ascii=False),
                "risk_json": None if group_name == "rule" else "[]",
            }
        )
    return pd.DataFrame(rows)


def _append_group_row(
    rows: pd.DataFrame, group_name: str, ts_code: str
) -> pd.DataFrame:
    extra = rows[rows["group_name"] == group_name].iloc[[0]].copy()
    extra["ts_code"] = ts_code
    extra["rank"] = 2
    return pd.concat([rows, extra], ignore_index=True)


def _primary_key(store: Store, table: str) -> list[str]:
    row = store.con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
        [table],
    ).fetchone()
    assert row is not None
    return list(row[0])


def _columns(store: Store, table: str) -> set[str]:
    return {
        row[1]
        for row in store.con.execute(f"PRAGMA table_info('{table}')").fetchall()
    }


def test_experiment_tables_and_primary_keys_exist(tmp_path):
    with Store(tmp_path / "schema.duckdb") as store:
        tables = {
            row[0]
            for row in store.con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        assert {"daily_limit", "experiment_runs", "experiment_decisions"} <= tables
        assert _primary_key(store, "daily_limit") == ["ts_code", "trade_date"]
        assert _primary_key(store, "experiment_runs") == ["run_id"]
        assert _primary_key(store, "experiment_decisions") == [
            "run_id",
            "group_name",
            "ts_code",
        ]
        assert _columns(store, "experiment_runs") == RUN_COLUMNS
        assert _columns(store, "experiment_decisions") == DECISION_COLUMNS


def test_four_groups_commit_in_one_transaction(tmp_path):
    with Store(tmp_path / "success.duckdb") as store:
        store.record_experiment(_run_row("r1"), _decisions("r1"))

        run = store.experiment_run("r1")
        assert run is not None
        assert run["status"] == "succeeded"
        assert run["finished_at"] is None
        saved = store.experiment_decisions("r1")
        assert set(saved["group_name"]) == GROUPS
        assert len(saved) == 4
        assert saved["group_name"].tolist() == ["ai", "benchmark", "hybrid", "rule"]


@pytest.mark.parametrize("case", ["extra", "mixed_run_id"])
def test_invalid_group_batch_rolls_back_without_marking_success(tmp_path, case):
    run_id = f"bad-{case}"
    rows = _decisions(run_id)
    if case == "extra":
        extra = rows.iloc[[0]].copy()
        extra["group_name"] = "other"
        extra["ts_code"] = "999999.SZ"
        rows = pd.concat([rows, extra], ignore_index=True)
    else:
        rows.loc[rows.index[0], "run_id"] = "another-run"

    with Store(tmp_path / f"{case}.duckdb") as store:
        store.create_experiment_run(_run_row(run_id))
        with pytest.raises(ValueError):
            store.record_experiment(_run_row(run_id), rows)

        assert store.experiment_decisions(run_id).empty
        assert store.experiment_run(run_id)["status"] == "running"


def test_partial_group_batch_commits_available_groups(tmp_path):
    run_id = "partial-groups"
    rows = _decisions(run_id)
    rows = rows[rows["group_name"].isin({"rule", "benchmark"})].copy()
    run_row = _run_row(run_id)
    run_row["model"] = None

    with Store(tmp_path / "partial-groups.duckdb") as store:
        store.record_experiment(run_row, rows)

        assert store.experiment_run(run_id)["status"] == "succeeded"
        assert set(store.experiment_decisions(run_id)["group_name"]) == {
            "rule",
            "benchmark",
        }


def test_database_error_rolls_back_run_and_decisions(tmp_path):
    rows = _decisions("db-error")
    rows["rank"] = rows["rank"].astype(object)
    rows.loc[rows.index[0], "rank"] = "not-an-integer"

    with Store(tmp_path / "db-error.duckdb") as store:
        with pytest.raises(duckdb.ConversionException, match="not-an-integer"):
            store.record_experiment(_run_row("db-error"), rows)

        assert store.experiment_run("db-error") is None
        assert store.experiment_decisions("db-error").empty
        leaked = store.con.execute(
            "SELECT view_name FROM duckdb_views() "
            "WHERE view_name IN "
            "('_stg_experiment_decisions', '_experiment_decisions_stage')"
        ).fetchall()
        assert leaked == []


@pytest.mark.parametrize(
    ("case", "candidate_count", "final_count"),
    [
        ("selected-extra", 2, 1),
        ("benchmark-extra", 1, 1),
    ],
)
def test_group_row_counts_must_match_run_metadata(
    tmp_path, case, candidate_count, final_count
):
    run_id = f"count-{case}"
    run_row = _run_row(
        run_id,
        candidate_count=candidate_count,
        final_count=final_count,
    )
    rows = _decisions(run_id)
    if case == "selected-missing":
        rows = _append_group_row(rows, "ai", "100001.SZ")
        rows = _append_group_row(rows, "hybrid", "100002.SZ")
        rows = _append_group_row(rows, "benchmark", "100003.SZ")
    elif case == "selected-extra":
        rows = _append_group_row(rows, "rule", "100001.SZ")
        rows = _append_group_row(rows, "benchmark", "100002.SZ")
    elif case == "benchmark-extra":
        rows = _append_group_row(rows, "benchmark", "100001.SZ")

    with Store(tmp_path / f"{case}.duckdb") as store:
        store.create_experiment_run(run_row)
        with pytest.raises(ValueError, match="行数"):
            store.record_experiment(run_row, rows)

        assert store.experiment_run(run_id)["status"] == "running"
        assert store.experiment_decisions(run_id).empty


def test_duplicate_ts_code_within_group_is_rejected_before_transaction(tmp_path):
    run_row = _run_row("duplicate", candidate_count=2)
    rows = _append_group_row(_decisions("duplicate"), "benchmark", "000004.SZ")

    with Store(tmp_path / "duplicate.duckdb") as store:
        store.create_experiment_run(run_row)
        with pytest.raises(ValueError, match="重复"):
            store.record_experiment(run_row, rows)

        assert store.experiment_run("duplicate")["status"] == "running"
        assert store.experiment_decisions("duplicate").empty


def test_record_rejects_outer_transaction_without_stealing_it_or_leaking_stage(
    tmp_path,
):
    with Store(tmp_path / "outer-transaction.duckdb") as store:
        store.con.execute("BEGIN TRANSACTION")
        store.con.execute(
            "INSERT INTO daily_limit VALUES ('000001.SZ', '20260804', 11.0, 9.0)"
        )

        with pytest.raises(RuntimeError, match="外层事务"):
            store.record_experiment(_run_row("nested"), _decisions("nested"))

        store.con.execute(
            "INSERT INTO daily_limit VALUES ('000002.SZ', '20260804', 12.0, 8.0)"
        )
        store.con.execute("COMMIT")
        assert store.con.execute("SELECT COUNT(*) FROM daily_limit").fetchone() == (2,)
        leaked = store.con.execute(
            "SELECT view_name FROM duckdb_views() "
            "WHERE view_name LIKE '%experiment_decisions%'"
        ).fetchall()
        assert leaked == []


def test_duplicate_record_is_idempotent_and_does_not_overwrite_success(tmp_path):
    with Store(tmp_path / "idempotent.duckdb") as store:
        store.record_experiment(_run_row("same"), _decisions("same"))
        store.record_experiment(_run_row("same"), _decisions("same"))

        assert len(store.experiment_decisions("same")) == 4
        assert store.create_experiment_run(
            {**_run_row("same", status="queued"), "candidate_count": 999}
        ) is False
        saved = store.experiment_run("same")
        assert saved["status"] == "succeeded"
        assert saved["candidate_count"] == 1


def _task_completion(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "now": "2026-08-04T15:40:00+08:00",
        "trade_date": "20260804",
        "result_json": json.dumps({"run_id": task_id}),
    }


def _scan_completion(run_id: str, *, selected: bool = True) -> dict:
    scan_run = {
        "run_id": run_id,
        "run_date": "20260804153200",
        "as_of": "20260804",
        "strategy": "hermes",
        "candidate_count": 1,
        "scored_count": 1,
        "passed_count": int(selected),
        "final_count": int(selected),
        "top_industries_json": "[]",
    }
    scan_rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "ts_code": "000001.SZ",
                "name": "样本",
                "industry": "测试",
                "rank": 1,
                "total": 1.0,
                "passed": selected,
                "selected": selected,
                "gate_reasons_json": "[]",
                "cat_scores_json": "{}",
                "money_class": "确认",
                "one_line": "样本",
                "contrib_json": "{}",
                "feat_json": "{}",
            }
        ]
    )
    picks = pd.DataFrame(
        [
            {
                "run_date": "20260804",
                "as_of": "20260804",
                "strategy": "hermes",
                "ts_code": "000001.SZ",
                "name": "样本",
                "industry": "测试",
                "rank": 1,
                "total": 1.0,
                "money_class": "确认",
                "one_line": "样本",
                "contrib_json": "{}",
                "feat_json": "{}",
            }
        ]
        if selected
        else []
    )
    return {
        "run_row": scan_run,
        "rows": scan_rows,
        "picks": picks,
        "as_of": "20260804",
        "strategy": "hermes",
    }


def test_experiment_and_task_success_commit_in_one_transaction(tmp_path):
    with Store(tmp_path / "atomic-success.duckdb") as store:
        claimed, _ = store.claim_task(
            task_id="atomic-success",
            kind="one_click_pipeline",
            trade_date="20260804",
            strategy="hermes",
            now="2026-08-04T15:31:00+08:00",
        )
        assert claimed is True
        store.mark_task_running("atomic-success", "2026-08-04T15:32:00+08:00")
        store.create_experiment_run(_run_row("atomic-success"))

        store.record_experiment(
            _run_row("atomic-success"),
            _decisions("atomic-success"),
            task_completion=_task_completion("atomic-success"),
            scan_completion=_scan_completion("atomic-success"),
        )

        experiment = store.experiment_run("atomic-success")
        task = store.get_task("atomic-success")
        assert experiment["status"] == "succeeded"
        assert len(store.experiment_decisions("atomic-success")) == 4
        assert task["status"] == "succeeded"
        assert task["trade_date"] == "20260804"
        assert json.loads(task["result_json"])["run_id"] == "atomic-success"
        assert store.latest_scan_run().iloc[0]["run_id"] == "atomic-success"
        assert store.con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 1


def test_rerunning_same_signal_date_keeps_every_batch_but_only_latest_picks(tmp_path):
    """同一信号日重跑：实验台账每次都留，picks 只留最后一次。

    这是两种保存语义并存的地方，也是最容易看错的地方——`picks` 里 8/21 只有 6 行会让人
    以为那天只跑了一次，实际跑了 6 次。

    - `experiment_decisions` 主键含 run_id，每次运行独立留存，台账才能按批次追溯。
    - `picks` 主键 (as_of, strategy, ts_code) 不含 run_id，同一信号日整组替换。它是回测与
      ML 训练的输入，要的是「每个交易日一份不重叠的名单」；同一天留 6 份会让同一笔钱被算
      6 次，净值直接虚高 6 倍。
    """
    with Store(tmp_path / "rerun.duckdb") as store:
        for index, run_id in enumerate(("first-run", "second-run", "third-run")):
            claimed, _ = store.claim_task(
                task_id=run_id,
                kind="one_click_pipeline",
                trade_date="20260804",
                strategy="hermes",
                now=f"2026-08-04T15:3{index}:00+08:00",
                # 幂等作用域是 (kind, trade_date, strategy)：已成功的任务会挡住同一信号日
                # 的后续运行。重跑走 force，与线上 POST /api/pipelines {"force": true} 一致。
                force=index > 0,
            )
            assert claimed, f"{run_id} 没抢到任务槽位"
            store.mark_task_running(run_id, f"2026-08-04T15:3{index}:30+08:00")
            store.create_experiment_run(_run_row(run_id))
            store.record_experiment(
                _run_row(run_id),
                _decisions(run_id),
                task_completion=_task_completion(run_id),
                scan_completion=_scan_completion(run_id),
            )

        # 累积：三个批次的元数据与四组名单都在。
        assert store.con.execute(
            "SELECT COUNT(*) FROM experiment_runs WHERE as_of = '20260804'"
        ).fetchone()[0] == 3
        assert store.con.execute(
            "SELECT COUNT(DISTINCT run_id) FROM experiment_decisions"
        ).fetchone()[0] == 3
        for run_id in ("first-run", "second-run", "third-run"):
            assert len(store.experiment_decisions(run_id)) == 4

        # 覆盖：picks 只剩最后一次那一份，不随重跑累积。
        picks = store.all_picks()
        assert len(picks) == 1, f"picks 累积了 {len(picks)} 行，回测净值会成倍虚高"
        # picks 刻意不带 run_id：加了就破坏「每个交易日一份不重叠」的去重保证。
        assert "run_id" not in set(picks.columns)


def test_failed_run_id_cannot_be_reused_for_a_new_batch(tmp_path):
    """失败批次的 run_id 不可复用：它的元数据不可信，重跑必须换新 run_id。

    允许复用会让一个 run_id 先记失败、后记成功，台账上分不清那次到底成没成。
    """
    with Store(tmp_path / "failed-reuse.duckdb") as store:
        store.create_experiment_run(_run_row("doomed"))
        store.fail_experiment_run(
            "doomed",
            "2026-08-04T15:40:00+08:00",
            json.dumps({"message": "上游断流"}),
        )

        with pytest.raises(ValueError, match="失败实验批次不可复用"):
            store.record_experiment(_run_row("doomed"), _decisions("doomed"))

        assert store.experiment_run("doomed")["status"] == "failed"
        assert len(store.experiment_decisions("doomed")) == 0


def test_missing_task_rolls_back_experiment_success(tmp_path):
    with Store(tmp_path / "atomic-rollback.duckdb") as store:
        store.create_experiment_run(_run_row("atomic-rollback"))

        with pytest.raises(KeyError, match="任务"):
            store.record_experiment(
                _run_row("atomic-rollback"),
                _decisions("atomic-rollback"),
                task_completion=_task_completion("atomic-rollback"),
                scan_completion=_scan_completion("atomic-rollback"),
            )

        assert store.experiment_run("atomic-rollback")["status"] == "running"
        assert store.experiment_decisions("atomic-rollback").empty
        assert store.latest_scan_run().empty
        assert store.con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 0


def test_empty_final_selection_clears_old_picks_in_atomic_completion(tmp_path):
    with Store(tmp_path / "empty-picks.duckdb") as store:
        old = _scan_completion("old-success")
        store.record_scan(old["run_row"], old["rows"])
        store.replace_picks(old["as_of"], old["strategy"], old["picks"])
        claimed, _ = store.claim_task(
            task_id="empty-final",
            kind="one_click_pipeline",
            trade_date="20260804",
            strategy="hermes",
            now="2026-08-04T15:31:00+08:00",
            force=True,
        )
        assert claimed is True
        store.mark_task_running("empty-final", "2026-08-04T15:32:00+08:00")
        store.create_experiment_run(_run_row("empty-final"))

        store.record_experiment(
            _run_row("empty-final"),
            _decisions("empty-final"),
            task_completion=_task_completion("empty-final"),
            scan_completion=_scan_completion("empty-final", selected=False),
        )

        assert store.con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 0
        assert store.latest_scan_run().iloc[0]["run_id"] == "empty-final"


def test_create_and_fail_run_enforce_status_transitions(tmp_path):
    with Store(tmp_path / "status.duckdb") as store:
        with pytest.raises(ValueError, match="queued|running"):
            store.create_experiment_run(_run_row("invalid", status="succeeded"))

        assert store.create_experiment_run(_run_row("failed")) is True
        store.fail_experiment_run(
            "failed",
            "2026-08-04T15:40:00+08:00",
            '{"type":"AIRequestError"}',
        )
        saved = store.experiment_run("failed")
        assert saved["status"] == "failed"
        assert saved["finished_at"] == "2026-08-04T15:40:00+08:00"
        assert saved["error_json"] == '{"type":"AIRequestError"}'


def test_succeeded_run_cannot_be_marked_failed(tmp_path):
    with Store(tmp_path / "succeeded.duckdb") as store:
        store.record_experiment(_run_row("succeeded"), _decisions("succeeded"))

        with pytest.raises(ValueError, match="成功"):
            store.fail_experiment_run(
                "succeeded",
                "2026-08-04T15:40:00+08:00",
                '{"type":"late-error"}',
            )

        saved = store.experiment_run("succeeded")
        assert saved["status"] == "succeeded"
        assert saved["error_json"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"as_of": ""},
        {"data_cutoff_at": None},
        {"strategy_name": "  "},
        {"strategy_version": None},
        {"prompt_version": ""},
        {"candidate_hash": ""},
        {"candidate_count": 0},
        {"final_count": 0},
        {"candidate_count": 1, "final_count": 2},
        {"temperature": None},
        {"hybrid_rule_weight": -0.1, "hybrid_ai_weight": 1.1},
        {"hybrid_rule_weight": 0.4, "hybrid_ai_weight": 0.5},
    ],
)
def test_run_audit_metadata_is_strictly_validated(tmp_path, overrides):
    row = {**_run_row("invalid-audit"), **overrides}
    with Store(tmp_path / "invalid-audit.duckdb") as store:
        with pytest.raises(ValueError):
            store.create_experiment_run(row)


@pytest.mark.parametrize(
    ("field", "changed"),
    [("as_of", "20260805"), ("candidate_hash", "sha256:different")],
)
def test_existing_run_rejects_mismatched_audit_metadata(tmp_path, field, changed):
    run_row = _run_row("audit-mismatch")
    with Store(tmp_path / f"audit-{field}.duckdb") as store:
        store.create_experiment_run(run_row)

        with pytest.raises(ValueError, match="审计"):
            store.record_experiment(
                {**run_row, field: changed}, _decisions("audit-mismatch")
            )

        saved = store.experiment_run("audit-mismatch")
        assert saved["status"] == "running"
        assert saved[field] == run_row[field]
        assert store.experiment_decisions("audit-mismatch").empty


def test_json_and_null_values_are_preserved_without_defaults(tmp_path):
    rows = _decisions("nulls")
    rows.loc[rows["group_name"] == "rule", "reason_json"] = None
    rows.loc[rows["group_name"] == "ai", "risk_json"] = None
    with Store(tmp_path / "nulls.duckdb") as store:
        store.record_experiment(_run_row("nulls"), rows)
        saved = store.experiment_decisions("nulls")

        rule = saved[saved["group_name"] == "rule"].iloc[0]
        ai = saved[saved["group_name"] == "ai"].iloc[0]
        assert pd.isna(rule["reason_json"])
        assert pd.isna(ai["risk_json"])


def test_missing_optional_values_are_stored_as_null(tmp_path):
    rows = _decisions("missing-optional")[["run_id", "group_name", "ts_code"]]
    with Store(tmp_path / "missing-optional.duckdb") as store:
        store.record_experiment(_run_row("missing-optional"), rows)
        saved = store.experiment_decisions("missing-optional")

        assert saved["reason_json"].isna().all()
        assert saved["risk_json"].isna().all()


def _returns_row(
    run_id: str,
    group_name: str,
    ts_code: str,
    *,
    horizon: str = "t1_close",
    entry_price: float | None = None,
    status: str = "pending_entry",
) -> dict:
    return {
        "run_id": run_id,
        "group_name": group_name,
        "ts_code": ts_code,
        "horizon": horizon,
        "entry_date": "20260805",
        "entry_price": entry_price,
        "sell_date": None,
        "sell_session": "close",
        "sell_price": None,
        "status": status,
        "reason": None,
        "gross_return": None,
        "created_at": "2026-08-05T18:00:00+08:00",
        "updated_at": "2026-08-05T18:00:00+08:00",
    }


def test_entries_awaiting_limits_only_lists_unsettled_succeeded_decisions(tmp_path):
    with Store(tmp_path / "awaiting.duckdb") as store:
        store.record_experiment(_run_row("done"), _decisions("done"))
        # 未成功的批次不参与补数据：状态还在 running，明细模拟中断现场手工写入。
        store.create_experiment_run(_run_row("running-run"))
        store.con.execute(
            "INSERT INTO experiment_decisions (run_id, group_name, ts_code, rank) "
            "VALUES ('running-run', 'rule', '000009.SZ', 1)"
        )
        store.upsert_experiment_returns(
            [
                # 买到了：entry_price 有值即为终局。
                _returns_row("done", "rule", "000001.SZ", entry_price=10.0, status="filled"),
                # 买不到：封板是市场事实，终局。
                _returns_row("done", "ai", "000002.SZ", status="entry_unavailable"),
                # 买入日没有 K 线：**不是**终局。每轮扫描只回补当轮候选池的日线，
                # 更早批次的票在后续买入日整片缺行，补上日线后仍要重算，
                # 所以必须继续出现在待补清单里。
                _returns_row("done", "hybrid", "000003.SZ", status="entry_bar_missing"),
                # 还没定：只有 pending_entry，需要继续补涨跌停价。
                _returns_row("done", "benchmark", "000004.SZ", status="pending_entry"),
            ]
        )

        awaiting = store.experiment_entries_awaiting_limits()

        assert list(awaiting.columns) == ["run_id", "as_of", "group_name", "ts_code"]
        assert awaiting.to_dict("records") == [
            {
                "run_id": "done",
                "as_of": "20260804",
                "group_name": "benchmark",
                "ts_code": "000004.SZ",
            },
            {
                "run_id": "done",
                "as_of": "20260804",
                "group_name": "hybrid",
                "ts_code": "000003.SZ",
            },
        ]


def test_experiment_returns_filter_by_as_of_and_entry_status(tmp_path):
    with Store(tmp_path / "returns-filters.duckdb") as store:
        store.record_experiment(_run_row("early"), _decisions("early"))
        store.record_experiment(
            _run_row("late", as_of="20260805"), _decisions("late")
        )
        store.upsert_experiment_returns(
            [
                _returns_row("early", "rule", "000001.SZ", entry_price=10.0, status="filled"),
                _returns_row("early", "ai", "000002.SZ", status="entry_unavailable"),
                _returns_row("early", "hybrid", "000003.SZ", status="pending_entry"),
                _returns_row("late", "rule", "000001.SZ", entry_price=11.0, status="filled"),
            ]
        )

        assert {row["group_name"] for row in store.experiment_returns(as_of="20260804")} == {
            "rule",
            "ai",
            "hybrid",
        }
        assert [row["run_id"] for row in store.experiment_returns(as_of="20260805")] == [
            "late"
        ]

        by_status = {
            entry_status: {
                (row["run_id"], row["group_name"])
                for row in store.experiment_returns(entry_status=entry_status)
            }
            for entry_status in ENTRY_STATUSES
        }
        assert by_status == {
            "filled": {("early", "rule"), ("late", "rule")},
            "entry_unavailable": {("early", "ai")},
            "pending_entry": {("early", "hybrid")},
        }
        assert store.experiment_returns(as_of="20260805", entry_status="pending_entry") == []


def test_entry_status_classification_and_predicate_share_one_contract():
    # 没算过收益和「算过但买不到」是两回事，不能合并成同一个状态。
    assert classify_entry_status([]) is None
    assert classify_entry_status([{"entry_price": 10.0, "status": "filled"}]) == "filled"
    assert (
        classify_entry_status([{"entry_price": None, "status": "entry_unavailable"}])
        == "entry_unavailable"
    )
    # 买入日没有 K 线是可修复的数据缺口，不是「买不到」。混进终局会让这些样本
    # 在补齐日线后也不再重算，收益永远算不出来。
    assert (
        classify_entry_status(
            [
                {"entry_price": None, "status": "pending_entry"},
                {"entry_price": None, "status": "entry_bar_missing"},
            ]
        )
        == "pending_entry"
    )
    assert (
        classify_entry_status([{"entry_price": None, "status": "pending_entry"}])
        == "pending_entry"
    )

    with pytest.raises(ValueError, match="非法成交状态"):
        entry_status_predicate("不存在的状态", alias="e")


_LEGACY_DECISIONS_DDL = """
CREATE TABLE experiment_decisions (
    run_id             VARCHAR,
    group_name         VARCHAR,
    ts_code            VARCHAR,
    name               VARCHAR,
    industry           VARCHAR,
    rank               INTEGER,
    rule_score         DOUBLE,
    ai_score           DOUBLE,
    hybrid_score       DOUBLE,
    reason_json        VARCHAR,
    risk_json          VARCHAR,
    entry_date         VARCHAR,
    entry_price        DOUBLE,
    entry_status       VARCHAR,
    entry_reason       VARCHAR,
    ret1               DOUBLE,
    ret1_target_date   VARCHAR,
    ret1_status        VARCHAR,
    ret1_reason        VARCHAR,
    PRIMARY KEY (run_id, group_name, ts_code)
)
"""


def test_opening_old_database_drops_legacy_decision_columns(tmp_path):
    path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(path))
    con.execute(_LEGACY_DECISIONS_DDL)
    con.execute(
        "INSERT INTO experiment_decisions VALUES "
        "('legacy', 'rule', '000001.SZ', '样本', '测试行业', 1, 88.0, NULL, NULL, "
        "NULL, NULL, '20260805', 10.0, 'filled', NULL, 0.02, '20260805', "
        "'succeeded', NULL)"
    )
    con.close()

    with Store(path) as store:
        columns = _columns(store, "experiment_decisions")
        assert columns == DECISION_COLUMNS
        assert columns.isdisjoint(_LEGACY_DECISION_COLUMNS)

        saved = store.experiment_decisions("legacy")
        assert len(saved) == 1
        row = saved.iloc[0]
        assert (row["run_id"], row["group_name"], row["ts_code"]) == (
            "legacy",
            "rule",
            "000001.SZ",
        )
        assert row["name"] == "样本"
        assert int(row["rank"]) == 1
        assert row["rule_score"] == pytest.approx(88.0)

    # 第二次开库已经没有旧列可删，迁移必须幂等。
    with Store(path) as store:
        assert _columns(store, "experiment_decisions") == DECISION_COLUMNS
        assert len(store.experiment_decisions("legacy")) == 1
