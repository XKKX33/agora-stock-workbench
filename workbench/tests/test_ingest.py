"""Tushare 截面摄取测试；客户端为本地假对象，不发网络请求。"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.db import Store
from engine.ingest_tushare import (
    _F_LIMIT,
    TushareClient,
    ingest_daily_limits,
    ingest_snapshot,
)


def test_stk_limit_uses_authoritative_endpoint_and_fields():
    seen = {}
    client = object.__new__(TushareClient)

    def fake_call(name, **kwargs):
        seen.update(name=name, kwargs=kwargs)
        return pd.DataFrame()

    client._call = fake_call
    client.stk_limit("20260804")

    assert seen == {
        "name": "stk_limit",
        "kwargs": {"trade_date": "20260804", "fields": _F_LIMIT},
    }
    assert _F_LIMIT == "ts_code,trade_date,up_limit,down_limit"


class _FakeClient:
    def __init__(self, limit_frames=None):
        self.limit_dates = []
        self.limit_frames = limit_frames or {}

    def daily(self, *, trade_date):
        return pd.DataFrame(
            [{"ts_code": "000001.SZ", "trade_date": trade_date, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2}]
        )

    def stock_basic(self):
        return pd.DataFrame([{"ts_code": "000001.SZ", "name": "样本"}])

    def daily_basic(self, trade_date):
        return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": trade_date, "turnover_rate": 2.0}])

    def stk_limit(self, trade_date):
        self.limit_dates.append(trade_date)
        if trade_date in self.limit_frames:
            return self.limit_frames[trade_date]
        return pd.DataFrame(
            [{"ts_code": "000001.SZ", "trade_date": trade_date, "up_limit": 11.0, "down_limit": 9.0}]
        )


def test_ingest_snapshot_upserts_daily_limit_and_reports_rows(tmp_path):
    fake = _FakeClient()
    with Store(tmp_path / "ingest.duckdb") as store:
        result = ingest_snapshot(store, fake, "20260804")
        saved = store.con.execute(
            "SELECT ts_code, trade_date, up_limit, down_limit FROM daily_limit"
        ).fetchall()

    assert fake.limit_dates == ["20260804"]
    assert result == {"daily": 1, "stock_basic": 1, "daily_basic": 1, "daily_limit": 1}
    assert saved == [("000001.SZ", "20260804", 11.0, 9.0)]


def _limit_frame(trade_date: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "up_limit": 11.0,
                "down_limit": 9.0,
            }
        ]
    )


def test_ingest_daily_limits_deduplicates_sorts_and_counts_real_rows(tmp_path):
    fake = _FakeClient(
        {
            "20260805": _limit_frame("20260805"),
            "20260806": _limit_frame("20260806"),
        }
    )
    with Store(tmp_path / "history.duckdb") as store:
        written = ingest_daily_limits(
            store, fake, ["20260806", "20260805", "20260806"]
        )
        saved = store.con.execute(
            "SELECT trade_date FROM daily_limit ORDER BY trade_date"
        ).fetchall()

    assert fake.limit_dates == ["20260805", "20260806"]
    assert written == 2
    assert saved == [("20260805",), ("20260806",)]


@pytest.mark.parametrize(
    "invalid",
    [
        pd.DataFrame(),
        _limit_frame("20260805").drop(columns=["ts_code"]),
        _limit_frame("20260805").drop(columns=["trade_date"]),
        _limit_frame("20260805").drop(columns=["up_limit"]),
        _limit_frame("20260805").drop(columns=["down_limit"]),
        _limit_frame("20260806"),
    ],
)
def test_ingest_daily_limits_rejects_invalid_response_without_writing(
    tmp_path, invalid
):
    fake = _FakeClient({"20260805": invalid})
    with Store(tmp_path / "invalid-history.duckdb") as store:
        with pytest.raises(RuntimeError, match="stk_limit"):
            ingest_daily_limits(store, fake, ["20260805"])
        saved_count = store.con.execute("SELECT COUNT(*) FROM daily_limit").fetchone()[0]

    assert saved_count == 0


def test_ingest_daily_limits_raises_on_later_failure_without_false_count(tmp_path):
    fake = _FakeClient(
        {"20260805": _limit_frame("20260805"), "20260806": pd.DataFrame()}
    )
    with Store(tmp_path / "partial-history.duckdb") as store:
        with pytest.raises(RuntimeError, match="20260806"):
            ingest_daily_limits(store, fake, ["20260805", "20260806"])
        saved = store.con.execute("SELECT trade_date FROM daily_limit").fetchall()

    assert fake.limit_dates == ["20260805", "20260806"]
    assert saved == [("20260805",)]


@pytest.mark.parametrize(
    "invalid",
    [pd.DataFrame(), _limit_frame("20260804").drop(columns=["up_limit"])],
)
def test_ingest_snapshot_rejects_invalid_daily_limit(tmp_path, invalid):
    fake = _FakeClient({"20260804": invalid})
    with Store(tmp_path / "invalid-snapshot.duckdb") as store:
        with pytest.raises(RuntimeError, match="stk_limit"):
            ingest_snapshot(store, fake, "20260804")
        saved_count = store.con.execute("SELECT COUNT(*) FROM daily_limit").fetchone()[0]

    assert saved_count == 0
