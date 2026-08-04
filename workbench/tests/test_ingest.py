"""Tushare 截面摄取测试；客户端为本地假对象，不发网络请求。"""

from __future__ import annotations

import pandas as pd

from engine.db import Store
from engine.ingest_tushare import _F_LIMIT, TushareClient, ingest_snapshot


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
    def __init__(self):
        self.limit_dates = []

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
