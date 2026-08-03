"""回测层单测:不重叠调仓、缺失跳过、换手成本、算不出返回 None。

全部用合成 DataFrame,不碰数据库。回测最容易出的错是**口径错**——
重叠持仓、把未回填当 0、把首期建仓的换手当成 0——这些都不抛异常,
只会让净值曲线变好看,所以必须逐条钉死。
"""

from __future__ import annotations

import os
import sys

import pandas as pd

_ENGINE_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ENGINE_PARENT not in sys.path:
    sys.path.insert(0, _ENGINE_PARENT)

from engine import backtest as BT  # noqa: E402


def _picks(days, *, strategy="s1", codes=("A", "B"), ret=0.01, horizon="ret5"):
    """每个截面等权两只票,收益给定。ret 可以是标量或 {day: value}。"""
    rows = []
    for day in days:
        value = ret[day] if isinstance(ret, dict) else ret
        for rank, code in enumerate(codes, start=1):
            rows.append(
                {
                    "as_of": day,
                    "strategy": strategy,
                    "ts_code": code,
                    "rank": rank,
                    horizon: value,
                }
            )
    return pd.DataFrame(rows)


def _days(n, start=1):
    """连续编号的假交易日:20260101 起,只用来排序和算跨度。"""
    return [f"202601{i:02d}" for i in range(start, start + n)]


def test_ret5_opens_one_position_every_five_sections():
    """不重叠是回测的第一条命:15 个截面在 ret5 下只能开 3 笔,不是 15 笔。"""
    days = _days(15)
    result = BT.run_backtest(_picks(days), horizon="ret5", strategy="s1")

    assert [p.as_of for p in result.periods] == [days[0], days[5], days[10]]
    assert result.scheduled_periods == 3
    assert result.available_days == 15
    # 覆盖率要如实给出:这条曲线只用了 1/5 的截面
    coverage = result.as_dict()["coverage"]
    assert coverage["measured_periods"] == 3
    assert coverage["available_days"] == 15


def test_first_period_counts_full_turnover_and_costs_it():
    """首期建仓的换手是 1.0。记成 0 就等于白拿了一次建仓成本。"""
    days = _days(15)
    result = BT.run_backtest(
        _picks(days), horizon="ret5", strategy="s1", cost_bps=30.0
    )

    first, second = result.periods[0], result.periods[1]
    assert first.turnover == 1.0
    assert round(first.net_return, 6) == round(0.01 - 0.003, 6)
    # 持仓没变 -> 不重复计费,净收益等于毛收益
    assert second.turnover == 0.0
    assert round(second.net_return, 6) == 0.01
    assert round(result.equity_curve[-1], 8) == round(1.007 * 1.01 * 1.01, 8)
    # 毛净两条都在,且毛的更高——成本口径可复核
    metrics = result.metrics()
    assert metrics["gross_total_return"] > metrics["total_return"]


def test_turnover_counts_only_changed_names():
    days = _days(10)
    base = _picks(days, codes=("A", "B"))
    # 第二个调仓日(下标 5)换掉一只:A 卖出、C 买入,B 留仓
    frame = pd.concat(
        [
            base[base["as_of"] != days[5]],
            _picks([days[5]], codes=("B", "C")),
        ],
        ignore_index=True,
    )
    result = BT.run_backtest(frame, horizon="ret5", strategy="s1")

    assert [p.as_of for p in result.periods] == [days[0], days[5]]
    assert result.periods[1].turnover == 0.5


def test_shrinking_basket_still_charges_the_exits():
    """篮子从 5 只缩到 3 只,3 只全留仓 —— 卖掉的两只必须收成本。

    这条是真的错过一次:旧公式 ``1 - kept / len(codes)`` 的分母是**新**篮子,
    5 只缩到 3 只时算出 ``1 - 3/3 = 0``,等于把清掉的 40% 仓位白送。
    原有的换手测试两期都是同样大小的篮子,所以这个方向一直没被覆盖。
    """
    days = _days(10)
    base = _picks(days, codes=("A", "B", "C", "D", "E"))
    # 第二个调仓日只剩 A/B/C:先删掉那天原本的 5 行,再拼回 3 行
    frame = pd.concat(
        [
            base[base["as_of"] != days[5]],
            _picks([days[5]], codes=("A", "B", "C")),
        ],
        ignore_index=True,
    )
    result = BT.run_backtest(frame, horizon="ret5", strategy="s1", top_k=5)

    second = result.periods[1]
    assert len(second.codes) == 3
    # 权重口径:卖掉 D/E 各 20%,留仓三只每只从 20% 补到 33.3%
    # sum|w_new - w_old| / 2 = (0.2 + 0.2 + 3 * 0.1333) / 2 = 0.4
    assert round(second.turnover, 6) == 0.4
    assert round(second.net_return, 6) == round(0.01 - 0.0030 * 0.4, 6)


def test_growing_basket_charges_the_same_as_the_mirror_shrink():
    """3 只加到 5 只与 5 只缩到 3 只的换手相等——权重口径对两个方向都成立。

    两边只能比到浮点精度:求和顺序来自 set 的迭代序,两次调用不一样,
    末位会差一个 ulp。这里要钉的是口径对称,不是逐位相等。
    """
    grow = BT._turnover(("A", "B", "C"), ("A", "B", "C", "D", "E"))
    shrink = BT._turnover(("A", "B", "C", "D", "E"), ("A", "B", "C"))

    assert round(grow, 10) == round(shrink, 10) == 0.4


def test_unfilled_return_skips_the_period_instead_of_counting_zero():
    """未回填不是 0 收益。整期跳过,并记下缺几只——用剩下的凑数会让口径悄悄变。"""
    days = _days(15)
    frame = _picks(days)
    frame.loc[frame["as_of"] == days[5], "ret5"] = float("nan")

    result = BT.run_backtest(frame, horizon="ret5", strategy="s1")

    assert [p.as_of for p in result.periods] == [days[0], days[10]]
    assert [(s.as_of, s.reason, s.n_missing) for s in result.skipped] == [
        (days[5], "return_not_backfilled", 2)
    ]
    # 跳过的那期在中间 -> 连乘时等于被当成 0 收益,这个事实要能被页面看见
    assert result.has_interior_gap is True


def test_schedule_does_not_shift_when_a_period_is_skipped():
    """跳过一期不能提前开下一笔,否则持仓就重叠了——不重叠是净值可信的前提。"""
    days = _days(15)
    frame = _picks(days)
    frame.loc[frame["as_of"] == days[5], "ret5"] = float("nan")

    result = BT.run_backtest(frame, horizon="ret5", strategy="s1")

    # 下一笔仍落在下标 10,而不是被提前到下标 6
    assert result.periods[1].as_of == days[10]


def test_trailing_gap_is_not_reported_as_interior():
    """末尾跳过是正常的(T+N 还没到),不该和中间有洞混为一谈。"""
    days = _days(15)
    frame = _picks(days)
    frame.loc[frame["as_of"] == days[10], "ret5"] = float("nan")

    result = BT.run_backtest(frame, horizon="ret5", strategy="s1")

    assert [p.as_of for p in result.periods] == [days[0], days[5]]
    assert len(result.skipped) == 1
    assert result.has_interior_gap is False


def test_top_k_takes_rank_order_not_row_order():
    """等权买 rank 前 K 名。按行序取会把打分最低的当成首选,而且不会报错。"""
    rows = [
        {"as_of": "20260101", "strategy": "s1", "ts_code": "C", "rank": 3, "ret5": -0.9},
        {"as_of": "20260101", "strategy": "s1", "ts_code": "A", "rank": 1, "ret5": 0.10},
        {"as_of": "20260101", "strategy": "s1", "ts_code": "B", "rank": 2, "ret5": 0.20},
    ]
    result = BT.run_backtest(pd.DataFrame(rows), horizon="ret5", top_k=2)

    period = result.periods[0]
    assert set(period.codes) == {"A", "B"}
    assert round(period.gross_return, 6) == 0.15


def test_max_drawdown_locates_peak_and_trough():
    drawdown, peak, trough = BT.max_drawdown([1.0, 1.2, 0.9, 1.0])

    assert round(drawdown, 6) == round(1 - 0.9 / 1.2, 6)
    assert (peak, trough) == (1, 2)


def test_max_drawdown_separates_no_drawdown_from_no_data():
    """一路上涨是 0.0(算出来没回撤),空序列是 None(算不出)。两者不能同值。"""
    assert BT.max_drawdown([1.0, 1.1, 1.2])[0] == 0.0
    assert BT.max_drawdown([])[0] is None


def test_single_period_reports_none_for_annualised_metrics():
    """一期样本上年化和夏普都是噪声,返回 None 而不是给个数字。"""
    result = BT.run_backtest(_picks(["20260101"]), horizon="ret5")
    metrics = result.metrics()

    assert metrics["n_periods"] == 1
    assert metrics["sharpe"] is None
    assert metrics["cagr"] is None
    # 全是盈利期时盈亏比为 inf,按算不出处理
    assert metrics["profit_factor"] is None
    assert metrics["max_drawdown"] == 0.0


def test_flat_returns_give_no_sharpe_but_still_give_cagr():
    """每期净收益完全一样 -> 标准差 0 -> 夏普算不出;年化仍然算得出。

    注意必须把成本设为 0 才真的"完全一样":首期要付一次建仓成本,
    带成本时第一期天然比后面低,序列就不是常数了。
    """
    days = _days(28)
    result = BT.run_backtest(_picks(days), horizon="ret5", cost_bps=0.0)
    metrics = result.metrics()

    assert metrics["n_periods"] == 6
    assert metrics["span_days"] >= BT.MIN_DAYS_FOR_CAGR
    assert metrics["sharpe"] is None
    assert metrics["cagr"] is not None


def test_inception_cost_alone_makes_the_first_period_differ():
    """成本不为 0 时首期必然低于后续期——建仓那一次费用不能被摊没。"""
    days = _days(28)
    result = BT.run_backtest(_picks(days), horizon="ret5", cost_bps=30.0)

    assert result.periods[0].net_return < result.periods[1].net_return



def test_mixed_returns_produce_sharpe_drawdown_and_profit_factor():
    days = _days(28)
    pattern = {day: (0.05 if i % 10 == 0 else -0.03) for i, day in enumerate(days)}
    result = BT.run_backtest(_picks(days, ret=pattern), horizon="ret5", cost_bps=0.0)
    metrics = result.metrics()

    assert metrics["n_periods"] == 6
    assert metrics["sharpe"] is not None
    assert metrics["profit_factor"] is not None
    assert metrics["max_drawdown"] > 0
    assert round(metrics["worst_period"], 6) == -0.03
    assert round(metrics["best_period"], 6) == 0.05
    assert round(metrics["win_rate"], 6) == round(3 / 6, 6)


def test_missing_horizon_column_is_named_not_guessed():
    result = BT.run_backtest(_picks(_days(5)), horizon="ret10")

    assert result.available is False
    assert result.missing_reason == "column_missing:ret10"
    assert result.metrics() == {}


def test_empty_ledger_reports_no_picks():
    result = BT.run_backtest(pd.DataFrame(), horizon="ret5")

    assert result.available is False
    assert result.missing_reason == "no_picks"


def test_all_periods_unmeasurable_is_distinct_from_empty_ledger():
    """有台账但一条都没回填,和根本没有台账是两件事,原因要分开。"""
    frame = _picks(_days(5))
    frame["ret5"] = float("nan")
    result = BT.run_backtest(frame, horizon="ret5")

    assert result.missing_reason == "no_measurable_period"
    assert result.available_days == 5
    assert len(result.skipped) == 1


def test_compare_strategies_runs_each_separately():
    days = _days(10)
    frame = pd.concat(
        [
            _picks(days, strategy="s1", ret=0.02),
            _picks(days, strategy="s2", ret=-0.01),
        ],
        ignore_index=True,
    )
    results = BT.compare_strategies(frame, horizon="ret5", cost_bps=0.0)

    assert [r.strategy for r in results] == ["s1", "s2"]
    assert results[0].metrics()["total_return"] > 0
    assert results[1].metrics()["total_return"] < 0


def test_compare_without_strategy_column_returns_nothing():
    frame = _picks(_days(5)).drop(columns=["strategy"])

    assert BT.compare_strategies(frame) == []


def test_unknown_horizon_raises_instead_of_defaulting():
    try:
        BT.run_backtest(_picks(_days(5)), horizon="ret7")
    except ValueError as error:
        assert "ret7" in str(error)
    else:
        raise AssertionError("未知期限必须抛错,不能悄悄退回默认期限")


def test_as_dict_carries_assumptions_and_curve_starting_at_one():
    days = _days(15)
    payload = BT.run_backtest(_picks(days), horizon="ret5", cost_bps=25.0).as_dict()

    assert payload["available"] is True
    # 成本与调仓口径是假设,单列一段,不混进 metrics 当事实
    assert payload["assumptions"]["cost_bps"] == 25.0
    assert payload["assumptions"]["mode"] == "non_overlap"
    # 曲线含起点 1.0,长度比期数多一
    curve = payload["equity_curve"]
    assert len(curve) == len(payload["periods"]) + 1
    assert curve[0]["equity"] == 1.0
    assert curve[0]["label"] == "起点"
    assert payload["drawdown"]["max"] is not None








def test_unmeasurable_result_has_no_curve_and_no_drawdown():
    """一期都没测出来时,曲线是空的、回撤是 None。

    这条是从线上真实负载里发现的:当时 equity_curve 固定带一个 1.0 起点,
    于是 max_drawdown 在"只有一个点"的曲线上算出 0.0——页面显示成
    "最大回撤 0.00%",看着像策略一路没跌,其实根本没测过任何一期。
    "算不出"和"算出来是 0"混在一起,而且混的方向刚好是让结果更好看。
    """
    days = _days(15)
    frame = _picks(days)
    frame["ret5"] = float("nan")

    result = BT.run_backtest(frame, horizon="ret5", strategy="s1")
    payload = result.as_dict()

    assert result.available is False
    assert result.missing_reason == "no_measurable_period"
    assert result.equity_curve == []
    assert result.gross_curve == []
    assert payload["equity_curve"] == []
    assert payload["drawdown"]["max"] is None
    assert payload["metrics"] == {}
    # 跳过的期次仍要如实列出:不可用不等于没有可交代的东西
    assert payload["coverage"]["skipped_periods"] == 3


def test_horizons_are_ordered_by_holding_days_not_by_name():
    """期限顺序按持仓天数,不按字符串。

    sorted(HORIZON_DAYS) 会得到 ret1 / ret10 / ret3 / ret5——ret10 插在中间。
    这个列表是接口负载的一部分,前端照着渲染下拉框就是错序。
    """
    assert BT.horizons() == ["ret1", "ret3", "ret5", "ret10"]
