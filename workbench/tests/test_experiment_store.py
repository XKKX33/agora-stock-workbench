"""实验批次存储测试：全部使用 tmp_path 临时 DuckDB。"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from engine.db import Store


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
    "entry_date",
    "entry_price",
    "entry_status",
    "entry_reason",
    "ret1",
    "ret1_target_date",
    "ret1_status",
    "ret1_reason",
    "ret3",
    "ret3_target_date",
    "ret3_status",
    "ret3_reason",
    "ret5",
    "ret5_target_date",
    "ret5_status",
    "ret5_reason",
    "ret10",
    "ret10_target_date",
    "ret10_status",
    "ret10_reason",
}


def _run_row(
    run_id: str,
    *,
    status: str = "running",
    candidate_count: int = 1,
    final_count: int = 1,
) -> dict:
    return {
        "run_id": run_id,
        "as_of": "20260804",
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
                "entry_date": None,
                "entry_price": None,
                "entry_status": "pending_entry",
                "entry_reason": None,
                "ret1": None,
                "ret1_target_date": None,
                "ret1_status": "future_not_reached",
                "ret1_reason": None,
                "ret3": None,
                "ret3_target_date": None,
                "ret3_status": "future_not_reached",
                "ret3_reason": None,
                "ret5": None,
                "ret5_target_date": None,
                "ret5_status": "future_not_reached",
                "ret5_reason": None,
                "ret10": None,
                "ret10_target_date": None,
                "ret10_status": "future_not_reached",
                "ret10_reason": None,
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


@pytest.mark.parametrize("case", ["missing", "extra", "mixed_run_id"])
def test_invalid_group_batch_rolls_back_without_marking_success(tmp_path, case):
    run_id = f"bad-{case}"
    rows = _decisions(run_id)
    if case == "missing":
        rows = rows[rows["group_name"] != "benchmark"].copy()
    elif case == "extra":
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
        ("selected-missing", 2, 2),
        ("selected-extra", 2, 1),
        ("benchmark-missing", 2, 1),
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
        {"model": ""},
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


def test_update_decision_uses_strict_field_whitelist(tmp_path):
    with Store(tmp_path / "update.duckdb") as store:
        store.record_experiment(_run_row("update"), _decisions("update"))
        store.update_experiment_decision(
            "update",
            "rule",
            "000001.SZ",
            entry_date="20260805",
            entry_price=10.0,
            entry_status="filled",
            ret1_target_date="20260805",
            ret1_status="succeeded",
            ret1=0.02,
        )
        saved = store.experiment_decisions("update")
        rule = saved[saved["group_name"] == "rule"].iloc[0]
        assert rule["entry_price"] == pytest.approx(10.0)
        assert rule["ret1"] == pytest.approx(0.02)

        with pytest.raises(ValueError, match="非法实验明细字段"):
            store.update_experiment_decision(
                "update", "rule", "000001.SZ", **{"status = 'hacked'": "x"}
            )
        assert store.experiment_run("update")["status"] == "succeeded"


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
        assert pd.isna(rule["entry_price"])
        assert pd.isna(rule["ret10"])


def test_missing_optional_values_are_stored_as_null(tmp_path):
    rows = _decisions("missing-optional")[["run_id", "group_name", "ts_code"]]
    with Store(tmp_path / "missing-optional.duckdb") as store:
        store.record_experiment(_run_row("missing-optional"), rows)
        saved = store.experiment_decisions("missing-optional")

        assert saved["reason_json"].isna().all()
        assert saved["risk_json"].isna().all()
        assert saved["entry_price"].isna().all()
        assert saved["ret10"].isna().all()


def test_pending_decisions_returns_rows_awaiting_entry_or_returns(tmp_path):
    with Store(tmp_path / "pending.duckdb") as store:
        store.record_experiment(_run_row("pending"), _decisions("pending"))
        pending = store.pending_experiment_decisions()
        assert len(pending) == 4
        assert set(pending["run_id"]) == {"pending"}

        for group_name, ts_code in pending[["group_name", "ts_code"]].itertuples(
            index=False, name=None
        ):
            store.update_experiment_decision(
                "pending",
                group_name,
                ts_code,
                entry_status="filled",
                ret1_status="succeeded",
                ret3_status="succeeded",
                ret5_status="succeeded",
                ret10_status="succeeded",
            )

        assert store.pending_experiment_decisions().empty
