"""离线全链路集成测试:合成数据直灌 DuckDB → run_scan(offline) → 校验。

不依赖 Tushare。验证:
- Store schema 建表 + upsert 幂等。
- snapshot/history/moneyflow_tail 的 <= as_of 过滤。
- apply_universe / industry_heat / seed_candidates 与打分链路联通。
- run_scan(online=False) 端到端产出 final 名单 + 写入 picks 台账。

运行:
    set PYTHONHOME=
    python workbench/tests/test_run_scan_offline.py
"""

from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import pandas as pd

_ENGINE_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ENGINE_PARENT not in sys.path:
    sys.path.insert(0, _ENGINE_PARENT)

from engine.db import Store  # noqa: E402
from engine.run_scan import run_scan, to_legacy_output  # noqa: E402


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ok: {name}")


# ---------------------------------------------------------------- 合成行情

_TRADE_DATES = pd.bdate_range(end="2025-08-12", periods=180).strftime("%Y%m%d").tolist()
AS_OF = _TRADE_DATES[-1]


def _daily_for(ts_code, start, drift, vol_mult, blowoff=False, seed=0):
    rng = np.random.RandomState(seed)
    n = len(_TRADE_DATES)
    steps = drift + rng.normal(0, 0.01, n)
    close = start * np.cumprod(1 + steps)
    if blowoff:
        close[-1] = close[-2] * 1.098
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    pre_close = np.r_[close[0], close[:-1]]
    pct_chg = (close / pre_close - 1) * 100
    vol = 1_000_000 * vol_mult * (1 + np.abs(rng.normal(0, 0.2, n)))
    if blowoff:
        vol[-1] = vol[-2] * 5.0
    amount = close * vol / 10.0  # 放大到"千元"量级,确保过 min_amount
    return pd.DataFrame({
        "ts_code": ts_code,
        "trade_date": _TRADE_DATES,
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "pct_chg": pct_chg,
        "vol": vol,
        "amount": amount,
    })


def _seed_db(store: Store):
    specs = [
        # ts_code, symbol, name, industry, start, drift, vol_mult, blowoff, seed
        # 起始价压低,使 160 日主升后仍 < price_max(70),避免被单价上限误杀
        ("600001.SH", "600001", "强主升A", "半导体", 8.0, 0.010, 1.4, False, 1),
        ("600002.SH", "600002", "强主升B", "半导体", 10.0, 0.009, 1.3, False, 2),
        ("600003.SH", "600003", "强主升C", "半导体", 7.0, 0.010, 1.2, False, 3),
        ("600010.SH", "600010", "温和D", "化工", 12.0, 0.004, 1.0, False, 4),
        ("600020.SH", "600020", "弱势E", "公用事业", 22.0, -0.006, 0.8, False, 5),
        ("600030.SH", "600030", "爆量涨停F", "消费电子", 11.0, 0.006, 1.1, True, 6),
        ("600040.SH", "600040", "题材G", "消费电子", 9.0, 0.009, 1.2, False, 7),
    ]
    basics, dailies, dbasics = [], [], []
    for ts_code, symbol, name, industry, start, drift, vm, blow, seed in specs:
        basics.append({
            "ts_code": ts_code, "symbol": symbol, "name": name,
            "area": "", "industry": industry, "market": "主板",
            "list_date": "20100101",
        })
        d = _daily_for(ts_code, start, drift, vm, blow, seed)
        dailies.append(d)
        last = d.iloc[-1]
        dbasics.append({
            "ts_code": ts_code, "trade_date": AS_OF,
            "turnover_rate": 6.0, "volume_ratio": 1.6,
            "total_mv": 5e6, "circ_mv": 4e6,
        })
    store.upsert("stock_basic", pd.DataFrame(basics), keys=("ts_code",))
    store.upsert("daily", pd.concat(dailies, ignore_index=True),
                 keys=("ts_code", "trade_date"))
    store.upsert("daily_basic", pd.DataFrame(dbasics), keys=("ts_code", "trade_date"))

    # 交易日历
    cal = pd.DataFrame({
        "exchange": "SSE", "cal_date": _TRADE_DATES, "is_open": 1,
    })
    store.upsert("trade_cal", cal, keys=("exchange", "cal_date"))

    # 资金流:强主升给"资金一致确认",弱势给"降级"
    mf_rows = []
    for ts_code, _s, _n, _i, _st, _d, _v, _b, _sd in specs:
        strong = ts_code.startswith("6000") and ts_code in (
            "600001.SH", "600002.SH", "600003.SH", "600040.SH")
        for td in _TRADE_DATES[-6:]:
            sign = 1.0 if strong else -1.0
            mf_rows.append({
                "ts_code": ts_code, "trade_date": td,
                "net_mf_amount": sign * 1000.0,
                "buy_lg_amount": 3000.0 if strong else 1000.0,
                "sell_lg_amount": 1000.0 if strong else 3000.0,
                "buy_elg_amount": 2000.0 if strong else 500.0,
                "sell_elg_amount": 500.0 if strong else 2000.0,
            })
    store.upsert("moneyflow", pd.DataFrame(mf_rows), keys=("ts_code", "trade_date"))


# ---------------------------------------------------------------- 测试

def test_store_pit_filters():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "t.duckdb")
        with Store(dbp) as store:
            _seed_db(store)
            # 幂等:再灌一次行数不翻倍
            _seed_db(store)
            snap = store.snapshot(AS_OF)
            _check("snapshot 行数=7", len(snap) == 7)
            _check("snapshot 含行业", snap["industry"].notna().all())

            # <= as_of 过滤:取一个中间日期,不应返回未来
            mid = _TRADE_DATES[100]
            hist = store.history("600001.SH", mid, 200)
            _check("history <= as_of 截断", hist["trade_date"].max() <= mid)

            mf = store.moneyflow_tail("600001.SH", AS_OF, 5)
            _check("moneyflow 尾部<=5", len(mf) <= 5)


def test_run_scan_offline_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "t.duckdb")
        with Store(dbp) as store:
            _seed_db(store)

        res = run_scan(strategy_name="strong_mainup", online=False,
                       db_path=dbp, record=True)

        _check("as_of 命中最新", res.as_of == AS_OF)
        _check("候选池非空", res.candidate_count > 0)
        _check("有股票入选", len(res.final) > 0)

        names = [s.name for s in res.final]
        print("    final:", names)

        # 强主升应入选,弱势不应入选；涨停本身不再作为硬剔除条件
        _check("强主升入选", any(n.startswith("强主升") for n in names))
        _check("弱势未入选", "弱势E" not in names)
        _check("涨停不因涨停本身被剔除", "爆量涨停F" in names)

        # 行业去重:半导体最多 2 只
        inds = [s.industry for s in res.final]
        _check("半导体去重<=2", inds.count("半导体") <= 2)

        # 每只都有一句话归因 + contrib
        for s in res.final:
            _check(f"{s.name} 有归因", len(s.one_line) > 0)
            _check(f"{s.name} 有contrib", len(s.contrib) > 0)

        # 台账已写入
        with Store(dbp) as store:
            picks = store.con.execute("SELECT * FROM picks").df()
            _check("picks 台账已写入", len(picks) == len(res.final))
            _check("picks 待回填收益为空", picks["ret5"].isna().all())

            runs = store.scan_runs()
            _check("扫描批次已写入", len(runs) == 1)
            _check("扫描批次ID一致", runs.iloc[0]["run_id"] == res.run_id)

            rows = store.scan_rows(res.run_id)
            _check("全部打分结果已写入", len(rows) == res.scored_count)
            _check("最终入选标记一致", int(rows["selected"].sum()) == len(res.final))
            _check("门槛结果同时存在", set(rows["passed"].tolist()) == {True, False})


def test_scan_batch_identity_is_reproducible_and_scope_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "t.duckdb")
        with Store(dbp) as store:
            _seed_db(store)

        first = run_scan(
            strategy_name="strong_mainup", online=False, db_path=dbp,
            record=True, as_of=AS_OF, overrides={"top_n": 3},
        )
        second = run_scan(
            strategy_name="strong_mainup", online=False, db_path=dbp,
            record=True, as_of=AS_OF, overrides={"top_n": 3},
        )
        assert second.run_id == first.run_id
        assert second.candidate_hash == first.candidate_hash
        assert second.config_hash == first.config_hash
        assert second.data_cutoff_at == first.data_cutoff_at
        with Store(dbp, ensure_schema=False) as store:
            runs = store.scan_runs()
            assert len(runs) == 1
            assert int(store.scan_rows(first.run_id).shape[0]) == first.scored_count
            run = store.scan_batch(
                first.run_id, as_of=AS_OF, strategy="strong_mainup"
            )[0]
            assert run["config_hash"] == first.config_hash

        other = run_scan(
            strategy_name="strong_mainup", online=False, db_path=dbp,
            record=True, as_of=AS_OF, overrides={"top_n": 2},
        )
        assert other.run_id != first.run_id
        with Store(dbp, ensure_schema=False) as store:
            assert len(store.scan_runs()) == 2
            assert len(store.scan_rows(first.run_id)) == first.scored_count


def test_legacy_output_preserves_stock_feature_contract():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "t.duckdb")
        with Store(dbp) as store:
            _seed_db(store)

        result = run_scan(
            strategy_name="strong_mainup",
            online=False,
            db_path=dbp,
            record=False,
            as_of=AS_OF,
            overrides={"top_n": 3},
        )
        output = to_legacy_output(result, price_max=70.0)

        assert output["final"]
        row = output["final"][0]
        assert {
            "weekly_bull",
            "ma_stack",
            "vol5_20",
            "vol20_60",
            "amt5_20",
            "vol_score",
            "net5",
            "big5",
            "big_pos_days",
            "money_class",
            "score",
        } <= set(row)
        assert "run_date" not in row
def test_scan_progress_callback_receives_real_stages(monkeypatch):
    from engine import run_scan as module

    events = []
    from types import SimpleNamespace

    events = []
    prepared = SimpleNamespace(candidates=["A", "B"])

    monkeypatch.setattr(module, "prepare_scan_data", lambda **kwargs: prepared)
    monkeypatch.setattr(module, "validate_scan_integrity", lambda value: {"candidate_count": 2})
    monkeypatch.setattr(module, "score_prepared_scan", lambda value, record=True: "result")

    result = module.run_scan(
        strategy_name="strong_mainup",
        online=False,
        db_path="unused.duckdb",
        record=False,
        as_of="20260817",
        on_progress=lambda **event: events.append(event),
    )

    assert result == "result"
    assert events[0]["stage"] == "prepare"
    assert events[-1]["stage"] == "score"
    assert all(event["step"] <= event["total"] for event in events)


def _run_all():
    for fn in (
        test_store_pit_filters,
        test_run_scan_offline_end_to_end,
        test_scan_batch_identity_is_reproducible_and_scope_isolated,
        test_legacy_output_preserves_stock_feature_contract,
    ):
        print(f"[{fn.__name__}]")
        fn()
    print("\nALL PASSED")


if __name__ == "__main__":
    _run_all()
