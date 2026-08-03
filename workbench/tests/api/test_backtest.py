"""回测接口测试。

夹具库里的 picks 全部没有回填 retN(见 test_analytics 的台账用例),
所以"没有可测期次"是这里的默认形态——接口必须把它说清,而不是回一条
从 1.0 开始的平线冒充净值。需要真实曲线的用例自己往库里写带 retN 的台账。
"""

from __future__ import annotations

import pandas as pd

from engine.db import Store

STRATEGY = "bt_demo"


def _days(n: int) -> list[str]:
    return [f"202601{i:02d}" for i in range(1, n + 1)]


def _seed_picks(db_path, days, *, strategy=STRATEGY, ret=0.01, codes=("600001.SH", "600002.SH")):
    rows = []
    for day in days:
        value = ret[day] if isinstance(ret, dict) else ret
        for rank, code in enumerate(codes, start=1):
            rows.append(
                {
                    "run_date": day,
                    "as_of": day,
                    "strategy": strategy,
                    "ts_code": code,
                    "name": code,
                    "rank": rank,
                    "total": 10.0 - rank,
                    "ret5": value,
                }
            )
    with Store(db_path, ensure_schema=True) as store:
        store.record_picks(pd.DataFrame(rows))


def test_unfilled_ledger_reports_no_measurable_period(client):
    """有台账但一期都没回填,要说"没有可测期次",不能给一条假曲线。"""
    payload = client.get("/api/backtest").json()

    assert payload["available"] is False
    assert payload["missing_reason"] == "no_measurable_period"
    assert payload["coverage"]["available_days"] > 0
    assert payload["coverage"]["measured_periods"] == 0
    assert payload["metrics"] == {}
    # 空曲线,不是一个 1.0 的假起点——单点曲线会让回撤算出 0.0(看着一路没跌)
    assert payload["equity_curve"] == []
    assert payload["drawdown"]["max"] is None


def test_options_are_served_so_the_page_need_not_hardcode_them(client):
    """可选期限和默认成本由接口给出。页面写死一份就会和 engine 悄悄分叉。"""
    payload = client.get("/api/backtest").json()

    # 按持仓天数排序,不是按字符串——按字符串 ret10 会插到 ret1 和 ret3 之间
    assert payload["horizons"] == ["ret1", "ret3", "ret5", "ret10"]
    assert payload["default_cost_bps"] == 30.0


def test_filled_ledger_returns_a_real_curve_with_assumptions(client, db_path):
    """回填过的台账要给出真曲线,并把成本/调仓口径作为假设一并带出。"""
    _seed_picks(db_path, _days(15))

    payload = client.get(f"/api/backtest?strategy={STRATEGY}").json()

    assert payload["available"] is True
    assert payload["coverage"] == {
        "available_days": 15,
        "scheduled_periods": 3,
        "measured_periods": 3,
        "skipped_periods": 0,
        "has_interior_gap": False,
    }
    curve = payload["equity_curve"]
    assert [point["label"] for point in curve] == ["起点", "20260101", "20260106", "20260111"]
    assert curve[0]["equity"] == 1.0
    # 首期付一次建仓成本(30bp × 换手 1.0),后两期持仓未变不重复计费
    assert curve[-1]["equity"] == round(1.007 * 1.01 * 1.01, 6)
    assert curve[-1]["gross_equity"] == round(1.01**3, 6)
    assert payload["assumptions"]["mode"] == "non_overlap"
    assert payload["assumptions"]["cost_bps"] == 30.0
    assert payload["metrics"]["n_periods"] == 3
    assert payload["metrics"]["win_rate"] == 1.0
    # 只有 3 期,夏普按"算不出"处理而不是给个数字
    assert payload["metrics"]["sharpe"] is None


def test_cost_bps_query_actually_changes_the_net_curve(client, db_path):
    """成本是可调假设。传 0 必须比默认 30bp 高,否则说明参数没接上。"""
    _seed_picks(db_path, _days(15))

    free = client.get(f"/api/backtest?strategy={STRATEGY}&cost_bps=0").json()
    charged = client.get(f"/api/backtest?strategy={STRATEGY}").json()

    assert free["assumptions"]["cost_bps"] == 0.0
    assert free["metrics"]["total_return"] > charged["metrics"]["total_return"]
    # 毛收益与成本无关,两次必须一致——否则是成本被算进了毛口径
    assert free["metrics"]["gross_total_return"] == charged["metrics"]["gross_total_return"]


def test_top_k_beyond_the_basket_does_not_invent_holdings(client, db_path):
    """台账每期只有 2 只,top_k=5 就只能买到 2 只,不能凑数。"""
    _seed_picks(db_path, _days(15))

    payload = client.get(f"/api/backtest?strategy={STRATEGY}&top_k=5").json()

    assert {period["n_holdings"] for period in payload["periods"]} == {2}


def test_compare_lists_one_row_per_strategy(client, db_path):
    """并排对比按策略分行。夹具自带的 strong_mainup 没回填,也要如实占一行。"""
    _seed_picks(db_path, _days(15), strategy="bt_win", ret=0.02)
    _seed_picks(db_path, _days(15), strategy="bt_lose", ret=-0.01)

    payload = client.get("/api/backtest/compare?cost_bps=0").json()

    items = {item["strategy"]: item for item in payload["items"]}
    assert {"bt_lose", "bt_win", "strong_mainup"} <= set(items)
    assert payload["available"] is True
    assert items["bt_win"]["metrics"]["total_return"] > 0
    assert items["bt_lose"]["metrics"]["total_return"] < 0
    assert items["strong_mainup"]["available"] is False
    assert items["strong_mainup"]["missing_reason"] == "no_measurable_period"
    # 对比行只带摘要,不带逐期持仓明细
    assert "periods" not in items["bt_win"]


def test_interior_gap_is_reported_not_smoothed_over(client, db_path):
    """中间一期没回填时,曲线是"已测期次连乘",这个事实要出现在 coverage 里。"""
    days = _days(15)
    returns = {day: (float("nan") if day == days[5] else 0.01) for day in days}
    _seed_picks(db_path, days, ret=returns)

    payload = client.get(f"/api/backtest?strategy={STRATEGY}").json()

    assert payload["coverage"]["measured_periods"] == 2
    assert payload["coverage"]["has_interior_gap"] is True
    assert payload["skipped"] == [
        {"as_of": days[5], "reason": "return_not_backfilled", "n_missing": 2}
    ]


def test_unknown_horizon_is_a_request_error_not_a_500(client):
    """非法期限是请求错,必须走统一错误体;500 会把排查方向指向服务端。"""
    response = client.get("/api/backtest?horizon=ret7")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_horizon"
    assert error["details"]["allowed"] == ["ret1", "ret3", "ret5", "ret10"]


def test_compare_validates_horizon_too(client):
    """对比接口不能漏掉同一道校验。"""
    response = client.get("/api/backtest/compare?horizon=ret7")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_horizon"


def test_out_of_range_top_k_is_rejected_by_validation(client):
    """top_k 有上下界,越界由 FastAPI 校验拦下,不进 engine。"""
    assert client.get("/api/backtest?top_k=0").status_code == 422
    assert client.get("/api/backtest?top_k=999").status_code == 422
    assert client.get("/api/backtest?cost_bps=-1").status_code == 422
