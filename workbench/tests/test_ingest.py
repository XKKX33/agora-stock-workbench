"""Tushare 截面摄取测试；客户端为本地假对象，不发网络请求。"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.db import Store
from engine.ingest_tushare import (
    _F_LIMIT,
    TushareClient,
    ingest_daily_limits,
    ingest_history,
    ingest_moneyflow,
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


def test_suspend_d_uses_authoritative_endpoint_and_fields():
    seen = {}
    client = object.__new__(TushareClient)

    def fake_call(name, **kwargs):
        seen.update(name=name, kwargs=kwargs)
        return pd.DataFrame()

    client._call = fake_call
    client.suspend_d("20260804")

    assert seen == {
        "name": "suspend_d",
        "kwargs": {
            "trade_date": "20260804",
            "fields": "ts_code,trade_date",
        },
    }


def test_stock_lifecycle_fetches_all_listing_statuses():
    calls = []
    client = object.__new__(TushareClient)

    def fake_call(name, **kwargs):
        calls.append((name, kwargs))
        return pd.DataFrame(
            [{
                "ts_code": f"{kwargs['list_status']}.SZ",
                "list_date": "20200101",
                "delist_date": None,
                "list_status": kwargs["list_status"],
            }]
        )

    client._call = fake_call
    frame = client.stock_lifecycle()

    assert [kwargs["list_status"] for _, kwargs in calls] == ["L", "D", "P"]
    assert {name for name, _ in calls} == {"stock_basic"}
    assert set(frame["list_status"]) == {"L", "D", "P"}


def test_tushare_failure_raises_without_printing_provider_exception(capsys):
    class BrokenPro:
        def daily(self, **_kwargs):
            raise RuntimeError("Authorization: Bearer AUDIT_SECRET_SENTINEL")

    client = object.__new__(TushareClient)
    client.pro = BrokenPro()
    client.retry = 1
    client.sleep = 0.0

    with pytest.raises(RuntimeError, match="Tushare daily 请求连续失败"):
        client._call("daily")

    captured = capsys.readouterr()
    assert "AUDIT_SECRET_SENTINEL" not in captured.out
    assert "AUDIT_SECRET_SENTINEL" not in captured.err

def test_tushare_retries_until_fifth_attempt():
    class FlakyPro:
        def __init__(self):
            self.calls = 0

        def daily(self, **_kwargs):
            self.calls += 1
            if self.calls < 5:
                raise RuntimeError("temporary rate limit")
            return pd.DataFrame([{"ts_code": "000001.SZ"}])

    client = object.__new__(TushareClient)
    client.pro = FlakyPro()
    client.retry = 5
    client.sleep = 0.0

    result = client._call("daily")

    assert len(result) == 1
    assert client.pro.calls == 5


def test_tushare_retries_none_response_instead_of_treating_it_as_empty():
    class FlakyPro:
        def __init__(self):
            self.calls = 0

        def daily(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return None
            return pd.DataFrame([{"ts_code": "000001.SZ"}])

    client = object.__new__(TushareClient)
    client.pro = FlakyPro()
    client.retry = 2
    client.sleep = 0.0

    result = client._call("daily")

    assert len(result) == 1
    assert client.pro.calls == 2


@pytest.mark.parametrize("operation", ["history", "moneyflow"])
def test_per_stock_ingest_continues_after_one_stock_fails(operation):
    class FakeStore:
        def __init__(self):
            self.saved = []

        def upsert(self, table, frame, *, keys):
            self.saved.append((table, frame.iloc[0]["ts_code"], keys))
            return len(frame)

    class FakeClient:
        def daily(self, *, ts_code, start_date, end_date):
            if ts_code == "BAD.SZ":
                raise RuntimeError("single stock failed")
            return pd.DataFrame([{"ts_code": ts_code, "trade_date": end_date}])

        def moneyflow(self, ts_code, start_date, end_date):
            if ts_code == "BAD.SZ":
                raise RuntimeError("single stock failed")
            return pd.DataFrame([{"ts_code": ts_code, "trade_date": end_date}])

    store = FakeStore()
    client = FakeClient()
    codes = ["GOOD1.SZ", "BAD.SZ", "GOOD2.SZ"]

    if operation == "history":
        total = ingest_history(store, client, codes, "20260801", "20260804")
    else:
        total = ingest_moneyflow(store, client, codes, "20260801", "20260804")

    assert total == 2
    assert [item[1] for item in store.saved] == ["GOOD1.SZ", "GOOD2.SZ"]


def test_settings_use_five_tushare_attempts():
    from engine.config import load_settings

    assert load_settings()["tushare"]["retry"] == 5

class _FakeClient:
    def __init__(self, limit_frames=None, suspend_frame=None):
        self.limit_dates = []
        self.limit_frames = limit_frames or {}
        self.suspend_dates = []
        self.suspend_frame = (
            pd.DataFrame(columns=["ts_code", "trade_date"])
            if suspend_frame is None
            else suspend_frame
        )

    def daily(self, *, trade_date):
        return pd.DataFrame(
            [{
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "pre_close": 10.0,
                "pct_chg": 2.0,
                "vol": 1000.0,
                "amount": 10200.0,
            }]
        )

    def stock_basic(self):
        return pd.DataFrame([{
            "ts_code": "000001.SZ",
            "symbol": "000001",
            "name": "样本",
            "area": "深圳",
            "industry": "银行",
            "market": "主板",
            "list_date": "19910403",
        }])

    def stock_lifecycle(self):
        return pd.DataFrame([{
            "ts_code": "000001.SZ",
            "list_date": "19910403",
            "delist_date": None,
            "list_status": "L",
        }])

    def daily_basic(self, trade_date):
        return pd.DataFrame([{
            "ts_code": "000001.SZ",
            "trade_date": trade_date,
            "turnover_rate": 2.0,
            "volume_ratio": 1.2,
            "total_mv": 100000.0,
            "circ_mv": 80000.0,
        }])

    def stk_limit(self, trade_date):
        self.limit_dates.append(trade_date)
        if trade_date in self.limit_frames:
            return self.limit_frames[trade_date]
        return pd.DataFrame(
            [{"ts_code": "000001.SZ", "trade_date": trade_date, "up_limit": 11.0, "down_limit": 9.0}]
        )

    def suspend_d(self, as_of):
        self.suspend_dates.append(as_of)
        return self.suspend_frame


def test_ingest_snapshot_upserts_daily_limit_and_reports_rows(tmp_path):
    fake = _FakeClient()
    with Store(tmp_path / "ingest.duckdb") as store:
        result = ingest_snapshot(store, fake, "20260804")
        saved = store.con.execute(
            "SELECT ts_code, trade_date, up_limit, down_limit FROM daily_limit"
        ).fetchall()
        suspended = store.con.execute(
            "SELECT ts_code, trade_date FROM suspend_daily"
        ).fetchall()
        lifecycle = store.con.execute(
            "SELECT ts_code, list_date, delist_date, list_status "
            "FROM security_lifecycle"
        ).fetchall()

    assert fake.limit_dates == ["20260804"]
    assert fake.suspend_dates == ["20260804"]
    assert result == {
        "daily": 1,
        "stock_basic": 1,
        "security_lifecycle": 1,
        "daily_basic": 1,
        "daily_limit": 1,
        "suspend_daily": 0,
    }
    assert saved == [("000001.SZ", "20260804", 11.0, 9.0)]
    assert suspended == []
    assert lifecycle == [("000001.SZ", "19910403", None, "L")]


def test_ingest_snapshot_preserves_missing_volume_ratio_for_new_stock(tmp_path):
    fake = _FakeClient()
    fake.daily_basic = lambda trade_date: _FakeClient().daily_basic(
        trade_date
    ).assign(volume_ratio=None)

    with Store(tmp_path / "nullable-volume-ratio.duckdb") as store:
        result = ingest_snapshot(store, fake, "20260804")
        saved = store.con.execute(
            "SELECT volume_ratio FROM daily_basic "
            "WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0]

    assert result["daily_basic"] == 1
    assert saved is None


def test_ingest_snapshot_rejects_negative_volume_ratio(tmp_path):
    fake = _FakeClient()
    fake.daily_basic = lambda trade_date: _FakeClient().daily_basic(
        trade_date
    ).assign(volume_ratio=-0.1)

    with Store(tmp_path / "negative-volume-ratio.duckdb") as store:
        with pytest.raises(RuntimeError, match="daily_basic.volume_ratio"):
            ingest_snapshot(store, fake, "20260804")


def test_ingest_snapshot_renames_and_persists_suspend_date(tmp_path):
    suspended = pd.DataFrame(
        [{"ts_code": "000001.SZ", "trade_date": "20260804"}]
    )
    fake = _FakeClient(suspend_frame=suspended)

    with Store(tmp_path / "suspend.duckdb") as store:
        result = ingest_snapshot(store, fake, "20260804")
        saved = store.con.execute(
            "SELECT ts_code, trade_date FROM suspend_daily"
        ).fetchall()
        with pytest.raises(Exception):
            store.con.execute(
                "INSERT INTO suspend_daily VALUES ('000001.SZ', '20260804')"
            )

    assert result["suspend_daily"] == 1
    assert saved == [("000001.SZ", "20260804")]


def test_ingest_snapshot_replaces_current_listed_stock_set(tmp_path):
    with Store(tmp_path / "listed.duckdb") as store:
        store.con.execute(
            "INSERT INTO stock_basic "
            "VALUES ('999999.SZ', '999999', '已退市', '', '', '', '20000101')"
        )

        ingest_snapshot(store, _FakeClient(), "20260804")
        saved_codes = store.con.execute(
            "SELECT ts_code FROM stock_basic ORDER BY ts_code"
        ).fetchall()

    assert saved_codes == [("000001.SZ",)]


@pytest.mark.parametrize(
    "invalid",
    [
        {"ts_code": "000001.SZ", "trade_date": "20260804"},
        pd.DataFrame([{"ts_code": "000001.SZ"}]),
        pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20260804"},
                {"ts_code": "000001.SZ", "trade_date": "20260804"},
            ]
        ),
        pd.DataFrame(
            [{"ts_code": "000001.SZ", "trade_date": "20260805"}]
        ),
    ],
    ids=["not-dataframe", "missing-field", "duplicate-key", "wrong-date"],
)
def test_ingest_snapshot_rejects_invalid_suspend_response_before_any_write(
    tmp_path, invalid
):
    fake = _FakeClient(suspend_frame=invalid)

    with Store(tmp_path / "invalid-suspend.duckdb") as store:
        with pytest.raises(RuntimeError, match="suspend_d"):
            ingest_snapshot(store, fake, "20260804")
        counts = {
            table: store.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "daily",
                "stock_basic",
                "daily_basic",
                "daily_limit",
                "suspend_daily",
            )
        }

    assert counts == {
        "daily": 0,
        "stock_basic": 0,
        "daily_basic": 0,
        "daily_limit": 0,
        "suspend_daily": 0,
    }


def test_ingest_snapshot_rolls_back_when_suspend_write_fails(tmp_path):
    db_path = tmp_path / "atomic-suspend.duckdb"
    original_suspend = pd.DataFrame(
        [{"ts_code": "000001.SZ", "trade_date": "20260804"}]
    )
    with Store(db_path) as store:
        ingest_snapshot(
            store,
            _FakeClient(suspend_frame=original_suspend),
            "20260804",
        )
        original_upsert = store.upsert

        def fail_suspend_write(table, frame, keys):
            if table == "suspend_daily":
                raise RuntimeError("停牌表写入失败")
            return original_upsert(table, frame, keys)

        store.upsert = fail_suspend_write
        changed = _FakeClient(
            suspend_frame=pd.DataFrame(columns=["ts_code", "trade_date"])
        )
        changed.daily = lambda *, trade_date: _FakeClient().daily(
            trade_date=trade_date
        ).assign(close=99.0)

        with pytest.raises(RuntimeError, match="停牌表写入失败"):
            ingest_snapshot(store, changed, "20260804")

        close = store.con.execute(
            "SELECT close FROM daily "
            "WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0]
        saved_suspend = store.con.execute(
            "SELECT ts_code, trade_date FROM suspend_daily"
        ).fetchall()

    assert close == 10.2
    assert saved_suspend == [("000001.SZ", "20260804")]


@pytest.mark.parametrize("endpoint", ["daily", "stock_basic", "daily_basic"])
def test_ingest_snapshot_rejects_empty_core_source_before_any_write(
    tmp_path, endpoint
):
    fake = _FakeClient()
    if endpoint == "daily":
        fake.daily = lambda *, trade_date: pd.DataFrame()
    elif endpoint == "stock_basic":
        fake.stock_basic = lambda: pd.DataFrame()
    else:
        fake.daily_basic = lambda trade_date: pd.DataFrame()

    with Store(tmp_path / f"empty-{endpoint}.duckdb") as store:
        with pytest.raises(RuntimeError, match=endpoint):
            ingest_snapshot(store, fake, "20260804")
        counts = {
            table: store.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("daily", "stock_basic", "daily_basic", "daily_limit")
        }

    assert counts == {
        "daily": 0,
        "stock_basic": 0,
        "daily_basic": 0,
        "daily_limit": 0,
    }


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


def test_ingest_snapshot_rolls_back_all_tables_when_last_write_fails(tmp_path):
    db_path = tmp_path / "atomic-snapshot.duckdb"
    with Store(db_path) as store:
        ingest_snapshot(store, _FakeClient(), "20260804")
        broken = _FakeClient()
        broken.daily = lambda *, trade_date: _FakeClient().daily(
            trade_date=trade_date
        ).assign(close=99.0)
        broken.daily_basic = lambda trade_date: _FakeClient().daily_basic(
            trade_date
        ).assign(turnover_rate="非法数值")

        with pytest.raises(Exception):
            ingest_snapshot(store, broken, "20260804")

        assert store.con.execute(
            "SELECT close FROM daily WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0] == 10.2
        assert store.con.execute(
            "SELECT turnover_rate FROM daily_basic "
            "WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0] == 2.0
        assert store.con.execute(
            "SELECT up_limit FROM daily_limit "
            "WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0] == 11.0


def test_duplicate_daily_limit_keys_do_not_delete_existing_value(tmp_path):
    db_path = tmp_path / "duplicate-limit.duckdb"
    duplicate = pd.concat(
        [_limit_frame("20260804"), _limit_frame("20260804").assign(up_limit=12.0)],
        ignore_index=True,
    )
    with Store(db_path) as store:
        ingest_daily_limits(store, _FakeClient(), ["20260804"])

        with pytest.raises(RuntimeError, match="重复"):
            ingest_daily_limits(
                store,
                _FakeClient({"20260804": duplicate}),
                ["20260804"],
            )

        assert store.con.execute(
            "SELECT up_limit FROM daily_limit "
            "WHERE ts_code='000001.SZ' AND trade_date='20260804'"
        ).fetchone()[0] == 11.0
