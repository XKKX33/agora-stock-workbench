"""自动复盘单测：合成 picks + daily，验证回填口径与统计正确性。

不依赖 Tushare。覆盖：
- retN 回填口径:base=as_of收盘, fut=as_of后第N个交易日收盘。
- 前视纪律:未来第N日未到 -> 保持 NULL(pending 计数)。
- IC 方向:total 与未来收益正相关时 IC>0。
- 胜率 / 盈亏比 / 分层收益计算正确。

运行:
    set PYTHONHOME=
    python workbench/tests/test_postmortem.py
"""

from __future__ import annotations

import os
import sys
import tempfile

import duckdb
import numpy as np
import pandas as pd

_ENGINE_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ENGINE_PARENT not in sys.path:
    sys.path.insert(0, _ENGINE_PARENT)

from engine.db import Store  # noqa: E402
from engine.postmortem import (  # noqa: E402
    backfill_returns, evaluate, run_postmortem, HORIZONS,
)


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ok: {name}")


# ---------------------------------------------------------------- 合成数据

# 20 个连续交易日(有行情的范围)
_DATES = [f"202506{d:02d}" for d in range(1, 21)]
# 日历刻意比行情多覆盖一段未来:线上 ingest 就是这么拉的
# (见 run_scan._calendar_lookahead_end),否则未到期样本会被误判成缺数据。
_FUTURE_DATES = [f"202507{d:02d}" for d in range(1, 11)]
_EXCH_CAL = pd.DataFrame({
    "exchange": "SSE",
    "cal_date": _DATES + _FUTURE_DATES,
    "is_open": 1,
})


def _seed(store: Store, *, as_of_idx: int, price_paths: dict):
    """price_paths: ts_code -> list[close](长度=len(_DATES))。写 daily + trade_cal。"""
    store.upsert("trade_cal", _EXCH_CAL, keys=("exchange", "cal_date"))
    rows = []
    for ts_code, closes in price_paths.items():
        for d, c in zip(_DATES, closes):
            rows.append({
                "ts_code": ts_code, "trade_date": d,
                "open": c, "high": c, "low": c, "close": c,
                "pre_close": c, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
            })
    store.upsert("daily", pd.DataFrame(rows), keys=("ts_code", "trade_date"))


def _seed_picks(store: Store, as_of: str, picks: list[dict]):
    rows = []
    for p in picks:
        rows.append({
            "run_date": as_of, "as_of": as_of, "strategy": "test",
            "ts_code": p["ts_code"], "name": p.get("name", p["ts_code"]),
            "industry": p.get("industry", "X"), "rank": p["rank"],
            "total": p["total"], "money_class": None, "one_line": "",
            "contrib_json": "{}", "feat_json": "{}",
            "ret1": None, "ret3": None, "ret5": None, "ret10": None,
        })
    store.record_picks(pd.DataFrame(rows))


# ---------------------------------------------------------------- 测试

def test_backfill_correctness():
    """as_of 选在第 5 日(idx4),未来 1/3/5/10 日应能回填。

    验证新口径:future_close 按市场交易日历定位,不按该票自己的 K 线数。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            # A 上涨:第5日=10, 第6日=11(+10%), 第8日=12, 第10日=13
            paths = {
                "A.SH": [10] * 20,
                "B.SH": [20] * 20,
            }
            # 手工设定 A 的未来价格
            a = paths["A.SH"][:]
            a[4] = 10.0   # as_of 基准(第5日)
            a[5] = 11.0   # +1 日 -> +10%
            a[7] = 12.0   # +3 日 -> +20%
            a[9] = 13.0   # +5 日 -> +30%
            # 第 15 日(+10日)= idx14
            a[14] = 15.0  # +10 日 -> +50%
            paths["A.SH"] = a

            _seed(store, as_of_idx=4, price_paths=paths)
            as_of = _DATES[4]
            _seed_picks(store, as_of, [
                {"ts_code": "A.SH", "rank": 1, "total": 0.9},
                {"ts_code": "B.SH", "rank": 2, "total": 0.5},
            ])

            report = backfill_returns(store, exchange="SSE")
            picks = store.all_picks("test").set_index("ts_code")

            _check("ret1 回填=+10%", abs(picks.loc["A.SH", "ret1"] - 0.10) < 1e-9)
            _check("ret3 回填=+20%", abs(picks.loc["A.SH", "ret3"] - 0.20) < 1e-9)
            _check("ret5 回填=+30%", abs(picks.loc["A.SH", "ret5"] - 0.30) < 1e-9)
            _check("ret10 回填=+50%", abs(picks.loc["A.SH", "ret10"] - 0.50) < 1e-9)
            _check("B 平盘 ret5=0", abs(picks.loc["B.SH", "ret5"] - 0.0) < 1e-9)
            _check("回填统计 ret5 计 2 条", report.filled["ret5"] == 2)
            _check("无缺数据待回填", sum(report.needs_attention().values()) == 0)


def test_lookahead_pending():
    """as_of 选在倒数第 2 日,未来第 5/10 日尚未到,应保持 NULL 且计 pending。

    验证 pending_reasons 能区分"未来未到"和"缺数据"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            paths = {"A.SH": [10 + i * 0.1 for i in range(20)]}
            _seed(store, as_of_idx=18, price_paths=paths)
            as_of = _DATES[18]  # 倒数第 2 日,后面只剩 1 个交易日
            _seed_picks(store, as_of, [{"ts_code": "A.SH", "rank": 1, "total": 0.8}])

            report = backfill_returns(store, exchange="SSE")
            picks = store.all_picks("test").set_index("ts_code")

            _check("ret1 可回填(后面还有1日)", not pd.isna(picks.loc["A.SH", "ret1"]))
            _check("ret3 未来未到->NULL", pd.isna(picks.loc["A.SH", "ret3"]))
            _check("ret5 未来未到->NULL", pd.isna(picks.loc["A.SH", "ret5"]))
            _check("ret3 计入 pending", report.pending["ret3"] == 1)
            _check("pending 原因是未来未到",
                   report.pending_reasons["ret3"].get("future_not_reached", 0) == 1)


def test_forward_calendar_keeps_pending_out_of_attention():
    """未到期的样本一律进 future_not_reached,needs_attention 必须为空。

    回归线上真实误报:ingest 曾把日历只拉到 as_of(end=as_of),于是
    sessions_after 永远排不出未来第 N 个开市日,正常等待被记成
    calendar_missing 并进了 needs_attention——每天报一堆"要人处理的缺数据",
    实际什么都不用做。判据改成"行情末日"后这类误报必须归零。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            paths = {"A.SH": [10 + i * 0.1 for i in range(20)]}
            _seed(store, as_of_idx=19, price_paths=paths)
            as_of = _DATES[19]  # 行情最后一日:1/3/5/10 日全都还没走到
            _seed_picks(store, as_of, [{"ts_code": "A.SH", "rank": 1, "total": 0.8}])

            report = backfill_returns(store, exchange="SSE")

            _check("四个期限全部待回填", sum(report.pending.values()) == 4)
            for col in HORIZONS:
                _check(f"{col} 归类未来未到",
                       report.pending_reasons[col].get("future_not_reached", 0) == 1)
            _check("不产生任何要人处理的缺数据",
                   sum(report.needs_attention().values()) == 0)


def test_short_calendar_still_reports_calendar_missing():
    """日历真的不够长时(旧 ingest 的样子),必须如实报 calendar_missing。

    把误报压掉不能变成"什么都不报":日历没回补是真要人处理的活,
    否则回填会一直静默地停在那里。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            short_cal = pd.DataFrame({
                "exchange": "SSE", "cal_date": _DATES, "is_open": 1,
            })
            store.upsert("trade_cal", short_cal, keys=("exchange", "cal_date"))
            rows = [{
                "ts_code": "A.SH", "trade_date": d,
                "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                "pre_close": 10.0, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
            } for d in _DATES]
            store.upsert("daily", pd.DataFrame(rows), keys=("ts_code", "trade_date"))
            _seed_picks(store, _DATES[19],
                        [{"ts_code": "A.SH", "rank": 1, "total": 0.8}])

            report = backfill_returns(store, exchange="SSE")

            _check("日历不够长时报 calendar_missing",
                   report.pending_reasons["ret1"].get("calendar_missing", 0) == 1)
            _check("日历该回补要进 needs_attention",
                   report.needs_attention().get("calendar_missing", 0) == 4)


def test_calendar_lookahead_covers_longest_horizon():
    """ingest 拉日历的末日必须真的越过最长回填期限,否则误报会复发。

    这是上面两个用例的上游:日历末日 = as_of 时,未到期样本无从判别。
    不复述实现里的算式(那样测的是"我抄对了没有"),改成校验实际结果:
    末日严格在未来,且窗口内的工作日在扣掉一整段连休后仍够 max(HORIZONS) 个。

    两处单位必须对齐,否则这条测试会自己算错:
    - 从**今天零点**起算,不从 datetime.now() 起算。末日是按日期取的,
      带上时分秒会让 (end_dt - now).days 少 1 天,于是过了午夜就少数一个工作日。
    - 连休余量要折成**工作日**再扣。_HOLIDAY_BUFFER_DAYS 是自然日,
      直接从工作日计数里减等于按 7/5 倍重罚,余量刚好时就会假报失败。
    """
    from datetime import datetime, timedelta

    from engine.run_scan import _HOLIDAY_BUFFER_DAYS, _calendar_lookahead_end

    end = _calendar_lookahead_end()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _check("末日格式为 YYYYMMDD", len(end) == 8 and end.isdigit())
    _check("末日严格在今天之后", end > today.strftime("%Y%m%d"))

    end_dt = datetime.strptime(end, "%Y%m%d")
    weekdays = sum(
        1 for i in range(1, (end_dt - today).days + 1)
        if (today + timedelta(days=i)).weekday() < 5
    )
    buffer_weekdays = _HOLIDAY_BUFFER_DAYS * 5 // 7
    max_sessions = max(HORIZONS.values())
    _check("扣掉一整段连休后仍覆盖最长期限",
           weekdays - buffer_weekdays >= max_sessions)


def test_evaluate_metrics():
    """构造 total 与未来收益正相关的多日样本,IC 应 > 0,统计合理。"""
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            # 两个交易日,每日 4 只票;分高的未来涨得多
            store.upsert("trade_cal", _EXCH_CAL, keys=("exchange", "cal_date"))
            codes = ["A.SH", "B.SH", "C.SH", "D.SH"]
            # 按 (ts_code, trade_date) 主键去重:先铺底(全 10),再让 as_of/fut 行覆盖
            daily_map: dict = {}
            for code in codes:
                for d in _DATES:
                    daily_map[(code, d)] = {
                        "ts_code": code, "trade_date": d, "open": 10, "high": 10,
                        "low": 10, "close": 10, "pre_close": 10, "pct_chg": 0.0,
                        "vol": 1e6, "amount": 1e7,
                    }
            pick_rows = []
            for di, as_of_idx in enumerate((2, 3)):  # 两个选股日
                as_of = _DATES[as_of_idx]
                fut = _DATES[as_of_idx + 5]  # +5 日
                for rank, code in enumerate(codes, start=1):
                    total = 1.0 - (rank - 1) * 0.2       # 0.8,0.6,0.4,0.2
                    fut_ret = (5 - rank) * 0.04           # rank1:+16%,rank4:+4%
                    base = 10.0 + di
                    daily_map[(code, as_of)] = {
                        "ts_code": code, "trade_date": as_of, "open": base,
                        "high": base, "low": base, "close": base,
                        "pre_close": base, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
                    }
                    daily_map[(code, fut)] = {
                        "ts_code": code, "trade_date": fut, "open": base,
                        "high": base, "low": base, "close": base * (1 + fut_ret),
                        "pre_close": base, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
                    }
                    pick_rows.append({
                        "run_date": as_of, "as_of": as_of, "strategy": "test",
                        "ts_code": code, "name": code, "industry": "X",
                        "rank": rank, "total": total, "money_class": None,
                        "one_line": "", "contrib_json": "{}", "feat_json": "{}",
                        "ret1": None, "ret3": None, "ret5": None, "ret10": None,
                    })
            store.upsert("daily", pd.DataFrame(list(daily_map.values())),
                         keys=("ts_code", "trade_date"))
            store.record_picks(pd.DataFrame(pick_rows))

            backfill_returns(store)
            stats = {s.horizon: s for s in evaluate(store, "test")}

            _check("有 ret5 统计", "ret5" in stats)
            s5 = stats["ret5"]
            _check("RankIC > 0(分高未来更强)", s5.rank_ic_mean > 0.5)
            _check("胜率=100%(全正收益)", abs(s5.win_rate - 1.0) < 1e-9)
            _check("rank1 平均收益 > rank4plus",
                   s5.layer_avg["rank1"] > s5.layer_avg["rank4plus"])
            print("    ret5 RankIC:", round(s5.rank_ic_mean, 3),
                  "分层:", {k: round(v, 3) for k, v in s5.layer_avg.items()})


def test_suspended_stock_uses_market_calendar():
    """停牌票的 retN 必须按市场第 N 个交易日,不能按它自己的第 N 根 K 线。

    旧实现数该票自己的后续 K 线,停牌 3 天时"第 5 根"实为市场第 8 日,
    retN 在不同股票间口径不一致,会污染横截面 IC。
    新实现:目标交易日无行情 -> 保持 NULL 并记 target_bar_missing。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            store.upsert("trade_cal", _EXCH_CAL, keys=("exchange", "cal_date"))
            as_of_idx = 4
            as_of = _DATES[as_of_idx]

            # NORMAL.SH 每日都有行情;SUSP.SH 在 as_of 后第 1~4 个交易日停牌
            rows = []
            for d_idx, d in enumerate(_DATES):
                rows.append({
                    "ts_code": "NORMAL.SH", "trade_date": d,
                    "open": 10, "high": 10, "low": 10, "close": 10.0,
                    "pre_close": 10, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
                })
                # 停牌区间:idx 5,6,7,8 无行情
                if as_of_idx < d_idx <= as_of_idx + 4:
                    continue
                # 基准 20,市场第 5 日(idx9)= 22 -> +10%
                close = 22.0 if d_idx == as_of_idx + 5 else 20.0
                rows.append({
                    "ts_code": "SUSP.SH", "trade_date": d,
                    "open": close, "high": close, "low": close, "close": close,
                    "pre_close": close, "pct_chg": 0.0, "vol": 1e6, "amount": 1e7,
                })
            store.upsert("daily", pd.DataFrame(rows), keys=("ts_code", "trade_date"))
            _seed_picks(store, as_of, [
                {"ts_code": "SUSP.SH", "rank": 1, "total": 0.9},
                {"ts_code": "NORMAL.SH", "rank": 2, "total": 0.5},
            ])

            report = backfill_returns(store, exchange="SSE")
            picks = store.all_picks("test").set_index("ts_code")

            # 市场第1个交易日(idx5)停牌 -> 不可得,保持 NULL(绝不用后面的价格顶替)
            _check("停牌票 ret1 保持 NULL", pd.isna(picks.loc["SUSP.SH", "ret1"]))
            _check("停牌记为 target_bar_missing",
                   report.pending_reasons["ret1"].get("target_bar_missing", 0) == 1)
            # 市场第5个交易日(idx9)已复牌 -> 按市场日历算 +10%
            _check("停牌票 ret5 按市场第5日=+10%",
                   abs(picks.loc["SUSP.SH", "ret5"] - 0.10) < 1e-9)
            _check("正常票 ret1 可回填", not pd.isna(picks.loc["NORMAL.SH", "ret1"]))


def test_picks_idempotent_by_as_of():
    """同一 as_of + strategy 重跑不得产生重复横截面。

    旧主键含墙钟 run_date:周五收盘跑一次、周六再跑一次,as_of 相同而
    run_date 不同 -> 同一只票两行 -> evaluate 把同一观测计两次,虚增样本。
    """
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            paths = {"A.SH": [10 + i * 0.2 for i in range(20)]}
            _seed(store, as_of_idx=4, price_paths=paths)
            as_of = _DATES[4]

            def _row(run_date: str, rank: int, total: float) -> pd.DataFrame:
                return pd.DataFrame([{
                    "run_date": run_date, "as_of": as_of, "strategy": "test",
                    "ts_code": "A.SH", "name": "A", "industry": "X",
                    "rank": rank, "total": total, "money_class": None,
                    "one_line": "", "contrib_json": "{}", "feat_json": "{}",
                    "ret1": None, "ret3": None, "ret5": None, "ret10": None,
                }])

            store.record_picks(_row("20250605", 1, 0.9))   # 当日收盘后跑
            store.record_picks(_row("20250606", 2, 0.7))   # 次日重跑同一 as_of

            picks = store.all_picks("test")
            _check("同一 as_of 只留 1 行", len(picks) == 1)
            _check("保留的是最后一次重跑结果",
                   abs(float(picks.iloc[0]["total"]) - 0.7) < 1e-9)

            # 不同 as_of 必须并存,不能被误删
            other = _row("20250606", 1, 0.8).assign(as_of=_DATES[5])
            store.record_picks(other)
            _check("不同 as_of 并存", len(store.all_picks("test")) == 2)


def test_picks_old_pk_migrates_to_business_key():
    """旧库主键 (run_date, strategy, ts_code) 在写路径打开时自动迁移。

    回归场景:同一 as_of 周五跑一次、周六重跑一次,旧主键下同一只票两行,
    evaluate 会把同一观测计两次;迁移后只保留 run_date 最新的一行,且新主键
    允许不同 as_of 的同 run_date 行并存。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.duckdb")
        con = duckdb.connect(path)
        con.execute("""
            CREATE TABLE picks (
                run_date VARCHAR, as_of VARCHAR, strategy VARCHAR, ts_code VARCHAR,
                name VARCHAR, industry VARCHAR, rank INTEGER, total DOUBLE,
                money_class VARCHAR, one_line VARCHAR, contrib_json VARCHAR,
                feat_json VARCHAR, ret1 DOUBLE, ret3 DOUBLE, ret5 DOUBLE, ret10 DOUBLE,
                PRIMARY KEY (run_date, strategy, ts_code)
            )
        """)
        con.execute("""
            INSERT INTO picks VALUES
            ('20250605', '20250605', 'test', 'A.SH', 'A', 'X', 1, 0.9, NULL, '', '{}', '{}', NULL, NULL, NULL, NULL),
            ('20250606', '20250605', 'test', 'A.SH', 'A', 'X', 2, 0.7, NULL, '', '{}', '{}', NULL, NULL, NULL, NULL)
        """)
        con.close()

        with Store(path) as store:  # ensure_schema=True -> 触发迁移
            picks = store.all_picks("test")
            _check("迁移后同一观测只剩 1 行", len(picks) == 1)
            _check("保留 run_date 最新的一行", abs(float(picks.iloc[0]["total"]) - 0.7) < 1e-9)
            # 新主键下同 run_date 不同 as_of 可以并存(业务键是 as_of,不是墙钟日)
            other = pd.DataFrame([{
                "run_date": "20250606", "as_of": "20250606", "strategy": "test",
                "ts_code": "A.SH", "name": "A", "industry": "X", "rank": 1,
                "total": 0.8, "money_class": None, "one_line": "",
                "contrib_json": "{}", "feat_json": "{}",
                "ret1": None, "ret3": None, "ret5": None, "ret10": None,
            }])
            store.record_picks(other)
            _check("不同 as_of 并存", len(store.all_picks("test")) == 2)

        con = duckdb.connect(path, read_only=True)
        pk = con.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'picks' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()
        _check("新主键为 (as_of, strategy, ts_code)", pk is not None and pk[0] == ["as_of", "strategy", "ts_code"])
        con.close()


def test_run_postmortem_json():
    with tempfile.TemporaryDirectory() as tmp:
        with Store(os.path.join(tmp, "t.duckdb")) as store:
            paths = {"A.SH": [10 + i * 0.2 for i in range(20)]}
            _seed(store, as_of_idx=4, price_paths=paths)
            _seed_picks(store, _DATES[4], [{"ts_code": "A.SH", "rank": 1, "total": 0.7}])
            summary = run_postmortem(store, "test")
            _check("摘要含 backfill", "backfill" in summary)
            _check("摘要含 stats", "stats" in summary)
            _check("backfill total_filled > 0", summary["backfill"]["total_filled"] > 0)


_TESTS = (
    test_backfill_correctness,
    test_lookahead_pending,
    test_forward_calendar_keeps_pending_out_of_attention,
    test_short_calendar_still_reports_calendar_missing,
    test_calendar_lookahead_covers_longest_horizon,
    test_suspended_stock_uses_market_calendar,
    test_picks_idempotent_by_as_of,
    test_picks_old_pk_migrates_to_business_key,
    test_evaluate_metrics,
    test_run_postmortem_json,
)


def _run_all():
    """逐个执行并统计通过/失败数量,失败不中断后续用例。

    不用 fn() 直接上抛:一个用例失败就看不到剩余用例结果,
    而任务要求"报告测试通过/失败数量",必须跑完全部才能给出真实计数。
    """
    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    for fn in _TESTS:
        print(f"[{fn.__name__}]")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 需汇总所有失败原因
            failed.append((fn.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"  FAILED: {type(exc).__name__}: {exc}")
        else:
            passed.append(fn.__name__)

    print(f"\n通过 {len(passed)} / 失败 {len(failed)}(共 {len(_TESTS)})")
    if failed:
        for name, reason in failed:
            print(f"  - {name}: {reason}")
        raise SystemExit(1)
    # 不用 emoji:Windows 控制台是 GBK,✅ 会抛 UnicodeEncodeError,
    # 让"全部通过"的一次运行以非零退出码结束,CI 里看起来像失败。
    print("ALL PASSED")


if __name__ == "__main__":
    _run_all()
