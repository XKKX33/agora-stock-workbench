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


def _run_row(run_id: str, *, status: str = "running") -> dict:
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
        "candidate_count": 20,
        "final_count": 1,
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
        assert saved["candidate_count"] == 20


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
