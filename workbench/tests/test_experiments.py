"""实验分组与收益回填测试；只使用临时 DuckDB。"""

from __future__ import annotations

import json
from copy import deepcopy

import pandas as pd
import pytest

from engine.db import Store
from engine.db_experiments import _DECISION_COLUMNS
from engine.experiments import (
    backfill_experiment_returns,
    build_experiment_decisions,
    candidate_pool_hash,
)


AS_OF = "20260804"
CODE = "000001.SZ"
SESSIONS = ["20260805", "20260806", "20260807", "20260808"]


def _scan_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ts_code": "A.SZ", "name": "甲", "industry": "一", "total": 100.0, "rank": 1},
            {"ts_code": "B.SZ", "name": "乙", "industry": "二", "total": 90.0, "rank": 2},
            {"ts_code": "C.SZ", "name": "丙", "industry": "二", "total": 90.0, "rank": 3},
            {"ts_code": "D.SZ", "name": "丁", "industry": "三", "total": 70.0, "rank": 4},
        ]
    )


def _agent_result() -> dict:
    return {
        "deep": [
            {"ts_code": "A.SZ", "score": 40.0, "points": ["甲要点"], "risks": []},
            {"ts_code": "B.SZ", "score": 70.0, "points": ["乙要点"], "risks": ["乙风险"]},
            {"ts_code": "C.SZ", "score": 70.0, "points": ["丙要点"], "risks": []},
            {"ts_code": "D.SZ", "score": 100.0, "points": ["丁要点"]},
        ],
        "final": [
            {"ts_code": "D.SZ", "score": 99.0, "thesis": "丁结论", "risks": ["丁风险"]},
            {"ts_code": "C.SZ", "score": 80.0, "thesis": "丙结论"},
            {"ts_code": "B.SZ", "score": 80.0, "thesis": "乙结论", "risks": []},
        ],
    }


def test_candidate_pool_hash_is_order_independent_and_member_sensitive():
    rows = _scan_rows()
    original = candidate_pool_hash(rows)
    reordered = candidate_pool_hash(rows.iloc[::-1].reset_index(drop=True))
    changed = candidate_pool_hash(rows.iloc[:-1])

    assert original == reordered
    assert original != changed
    assert len(original) == 64
    assert original == original.lower()


def test_builds_four_groups_with_stable_ranks_and_average_percentiles():
    pool_hash, decisions = build_experiment_decisions(
        "run-1", _scan_rows(), _agent_result(), final_count=3
    )

    assert pool_hash == candidate_pool_hash(_scan_rows())
    assert tuple(decisions.columns) == _DECISION_COLUMNS
    groups = {name: frame for name, frame in decisions.groupby("group_name", sort=False)}
    assert {name: len(frame) for name, frame in groups.items()} == {
        "rule": 3,
        "ai": 3,
        "hybrid": 3,
        "benchmark": 4,
    }
    assert groups["rule"].sort_values("rank")["ts_code"].tolist() == ["A.SZ", "B.SZ", "C.SZ"]
    assert groups["ai"].sort_values("rank")["ts_code"].tolist() == ["D.SZ", "B.SZ", "C.SZ"]
    # average 百分位下四只的混合分都为 0.625，并列时按代码稳定取前三。
    hybrid = groups["hybrid"].sort_values("rank")
    assert hybrid["ts_code"].tolist() == ["A.SZ", "B.SZ", "C.SZ"]
    assert hybrid["hybrid_score"].tolist() == pytest.approx([0.625, 0.625, 0.625])
    assert groups["benchmark"].sort_values("rank")["ts_code"].tolist() == [
        "A.SZ", "B.SZ", "C.SZ", "D.SZ"
    ]


def test_decisions_start_pending_without_invented_reasons_or_risks():
    _, decisions = build_experiment_decisions(
        "run-1", _scan_rows(), _agent_result(), final_count=3
    )

    assert set(decisions["entry_status"]) == {"pending_entry"}
    for horizon in (1, 3, 5, 10):
        assert set(decisions[f"ret{horizon}_status"]) == {"future_not_reached"}
        assert decisions[f"ret{horizon}"].isna().all()
        assert decisions[f"ret{horizon}_target_date"].isna().all()
        assert decisions[f"ret{horizon}_reason"].isna().all()
    ai_c = decisions[(decisions["group_name"] == "ai") & (decisions["ts_code"] == "C.SZ")].iloc[0]
    assert json.loads(ai_c["reason_json"])["thesis"] == "丙结论"
    assert pd.isna(ai_c["risk_json"])
    ai_b = decisions[(decisions["group_name"] == "ai") & (decisions["ts_code"] == "B.SZ")].iloc[0]
    assert json.loads(ai_b["risk_json"]) == []
    rule_a = decisions[(decisions["group_name"] == "rule") & (decisions["ts_code"] == "A.SZ")].iloc[0]
    assert pd.isna(rule_a["reason_json"])
    assert pd.isna(rule_a["risk_json"])


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda result: result.update(final=result["final"][:-1]), "final"),
        (lambda result: result["final"].__setitem__(1, {**result["final"][0]}), "final"),
        (lambda result: result["final"][0].update(ts_code="OUT.SZ"), "候选池"),
        (lambda result: result["final"][0].update(score=float("nan")), "score"),
        (lambda result: result.update(deep=result["deep"][:2]), "deep"),
        (lambda result: result["deep"][0].update(ts_code="OUT.SZ"), "候选池"),
        (lambda result: result["deep"][0].update(score=float("inf")), "score"),
    ],
)
def test_rejects_incomplete_or_invalid_agent_results(mutate, match):
    result = deepcopy(_agent_result())
    mutate(result)
    with pytest.raises(ValueError, match=match):
        build_experiment_decisions("run-1", _scan_rows(), result, final_count=3)


@pytest.mark.parametrize(
    "rule_weight,ai_weight",
    [(float("nan"), 0.5), (-0.1, 1.1), (0.2, 0.7), (True, 0.0)],
)
def test_rejects_invalid_hybrid_weights(rule_weight, ai_weight):
    with pytest.raises(ValueError, match="权重"):
        build_experiment_decisions(
            "run-1",
            _scan_rows(),
            _agent_result(),
            final_count=3,
            rule_weight=rule_weight,
            ai_weight=ai_weight,
        )


def test_rejects_invalid_frozen_candidate_pool():
    duplicated = pd.concat([_scan_rows(), _scan_rows().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="ts_code"):
        candidate_pool_hash(duplicated)
    missing = _scan_rows()
    missing.loc[0, "industry"] = None
    with pytest.raises(ValueError, match="industry"):
        build_experiment_decisions("run-1", missing, _agent_result(), final_count=3)


def _run_row(run_id: str, candidate_count: int = 1, final_count: int = 1) -> dict:
    return {
        "run_id": run_id,
        "as_of": AS_OF,
        "data_cutoff_at": "2026-08-04T15:30:00+08:00",
        "status": "running",
        "strategy_name": "test",
        "strategy_version": "v1",
        "model": "fake-model",
        "temperature": 0.0,
        "prompt_version": "p1",
        "candidate_hash": "a" * 64,
        "candidate_count": candidate_count,
        "final_count": final_count,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": "2026-08-04T15:31:00+08:00",
        "finished_at": "2026-08-04T15:32:00+08:00",
        "error_json": None,
    }


def _pending_decisions(run_id: str) -> pd.DataFrame:
    rows = []
    for group_name in ("rule", "ai", "hybrid", "benchmark"):
        row = {column: None for column in _DECISION_COLUMNS}
        row.update(
            run_id=run_id,
            group_name=group_name,
            ts_code=CODE,
            name="样本",
            industry="测试",
            rank=1,
            rule_score=80.0,
            entry_status="pending_entry",
        )
        for horizon in (1, 3, 5, 10):
            row[f"ret{horizon}_status"] = "future_not_reached"
        rows.append(row)
    return pd.DataFrame(rows, columns=_DECISION_COLUMNS)


def _seed_experiment(store: Store, run_id: str = "r1") -> None:
    store.record_experiment(_run_row(run_id), _pending_decisions(run_id))


def _seed_calendar(store: Store, sessions: list[str] = SESSIONS) -> None:
    rows = [{"exchange": "SSE", "cal_date": AS_OF, "is_open": 1}]
    rows.extend({"exchange": "SSE", "cal_date": date, "is_open": 1} for date in sessions)
    store.upsert("trade_cal", pd.DataFrame(rows), keys=("exchange", "cal_date"))


def _daily_row(
    trade_date: str,
    *,
    ts_code: str = CODE,
    open_price: float | None = 10.0,
    high: float | None = 10.5,
    low: float | None = 9.8,
    close: float | None = 10.0,
) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
    }


def _seed_limit(store: Store, up_limit: float | None = 11.0) -> None:
    store.upsert(
        "daily_limit",
        pd.DataFrame([{"ts_code": CODE, "trade_date": SESSIONS[0], "up_limit": up_limit, "down_limit": 9.0}]),
        keys=("ts_code", "trade_date"),
    )


def _saved(store: Store, run_id: str = "r1") -> pd.DataFrame:
    return store.experiment_decisions(run_id).set_index("group_name")


def test_backfill_uses_next_open_for_entry_and_market_session_targets(tmp_path):
    with Store(tmp_path / "normal.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store)
        store.upsert(
            "daily",
            pd.DataFrame(
                [
                    _daily_row(SESSIONS[0], close=11.0),
                    _daily_row(SESSIONS[1], close=12.0),
                    _daily_row(SESSIONS[2], close=13.0),
                ]
            ),
            keys=("ts_code", "trade_date"),
        )

        summary = backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_date"]) == {SESSIONS[0]}
        assert set(saved["entry_price"]) == {10.0}
        assert set(saved["entry_status"]) == {"filled"}
        assert saved.loc["rule", "ret1"] == pytest.approx(0.1)
        assert saved.loc["rule", "ret3"] == pytest.approx(0.3)
        assert saved.loc["rule", "ret1_target_date"] == SESSIONS[0]
        assert saved.loc["rule", "ret3_target_date"] == SESSIONS[2]
        assert summary.updated == 4
        assert summary.filled == 4
        assert summary.return_filled == 8


def test_backfill_keeps_future_entry_pending(tmp_path):
    with Store(tmp_path / "future.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        store.upsert("daily", pd.DataFrame([_daily_row(AS_OF, ts_code="MARKET.SH")]), keys=("ts_code", "trade_date"))

        summary = backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_date"]) == {SESSIONS[0]}
        assert set(saved["entry_status"]) == {"pending_entry"}
        assert set(saved["entry_reason"]) == {"future_not_reached"}
        assert summary.pending == 4


def test_backfill_reports_missing_calendar(tmp_path):
    with Store(tmp_path / "calendar.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store, sessions=[])

        backfill_experiment_returns(store)
        saved = _saved(store)

        assert saved["entry_date"].isna().all()
        assert set(saved["entry_status"]) == {"pending_entry"}
        assert set(saved["entry_reason"]) == {"calendar_missing"}


def test_backfill_rejects_missing_entry_bar(tmp_path):
    with Store(tmp_path / "entry-missing.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store)
        store.upsert("daily", pd.DataFrame([_daily_row(SESSIONS[2], ts_code="MARKET.SH")]), keys=("ts_code", "trade_date"))

        backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_status"]) == {"entry_unavailable"}
        assert set(saved["entry_reason"]) == {"entry_bar_missing"}
        assert set(saved["ret1_status"]) == {"entry_unavailable"}
        assert set(saved["ret1_reason"]) == {"entry_bar_missing"}


@pytest.mark.parametrize("open_price", [None, 0.0, -1.0, float("inf")])
def test_backfill_rejects_invalid_open(tmp_path, open_price):
    with Store(tmp_path / "invalid-open.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store)
        store.upsert(
            "daily",
            pd.DataFrame([_daily_row(SESSIONS[0], open_price=open_price), _daily_row(SESSIONS[2], ts_code="MARKET.SH")]),
            keys=("ts_code", "trade_date"),
        )

        backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_status"]) == {"entry_unavailable"}
        assert set(saved["entry_reason"]) == {"invalid_open"}
        assert saved["ret1"].isna().all()


def test_backfill_waits_for_authoritative_limit_price(tmp_path):
    with Store(tmp_path / "limit-missing.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        store.upsert("daily", pd.DataFrame([_daily_row(SESSIONS[0])]), keys=("ts_code", "trade_date"))

        backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_status"]) == {"pending_entry"}
        assert set(saved["entry_reason"]) == {"limit_price_missing"}


def test_backfill_rejects_locked_limit_up_with_fixed_tolerance(tmp_path):
    with Store(tmp_path / "locked.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store, up_limit=11.0)
        store.upsert(
            "daily",
            pd.DataFrame([_daily_row(SESSIONS[0], open_price=11.0, high=11.0, low=11.0, close=11.0)]),
            keys=("ts_code", "trade_date"),
        )

        summary = backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["entry_status"]) == {"entry_unavailable"}
        assert set(saved["entry_reason"]) == {"limit_up_locked"}
        assert summary.unavailable == 4


def test_backfill_marks_reached_target_with_invalid_close_missing(tmp_path):
    with Store(tmp_path / "target-missing.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store)
        store.upsert(
            "daily",
            pd.DataFrame(
                [
                    _daily_row(SESSIONS[0], close=10.0),
                    _daily_row(SESSIONS[2], ts_code="MARKET.SH"),
                ]
            ),
            keys=("ts_code", "trade_date"),
        )

        backfill_experiment_returns(store)
        saved = _saved(store)

        assert set(saved["ret3_status"]) == {"target_bar_missing"}
        assert set(saved["ret3_reason"]) == {"target_bar_missing"}
        assert saved["ret3"].isna().all()


def test_backfill_preserves_zero_return_and_is_idempotent(tmp_path):
    with Store(tmp_path / "zero.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_limit(store)
        store.upsert(
            "daily",
            pd.DataFrame([_daily_row(SESSIONS[0], close=10.0), _daily_row(SESSIONS[2], close=10.0)]),
            keys=("ts_code", "trade_date"),
        )

        first = backfill_experiment_returns(store)
        before = _saved(store).copy()
        second = backfill_experiment_returns(store)
        after = _saved(store)

        assert set(after["ret1"]) == {0.0}
        assert set(after["ret3"]) == {0.0}
        pd.testing.assert_frame_equal(before, after)
        assert first.return_filled == 8
        assert second.updated == 0
        assert second.return_filled == 0
