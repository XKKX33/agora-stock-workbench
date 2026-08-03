"""交易日闸门与调度配置的单元测试。

这些用例全部用固定时钟,不依赖"今天"是不是交易日——否则测试会在周末
自己变绿或变红,失去意义。闸门写成纯函数就是为了能这样穷举边界。

运行:
    python -m pytest workbench/tests/test_schedule.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.schedule import (  # noqa: E402
    ScheduleConfigError,
    decide_due_run,
    is_trading_day,
    load_schedule_config,
    normalize_trade_date,
    parse_run_after,
)

RUN_AFTER = time(15, 30)

# 2026-07-31 是周五(交易日),2026-08-01 周六休市
FRIDAY = "20260731"
SATURDAY = "20260801"


def _decide(now: datetime, *, latest_open: str | None, cal_max: str | None):
    return decide_due_run(
        now=now,
        run_after=RUN_AFTER,
        latest_open_date=latest_open,
        calendar_max=cal_max,
    )


# --------------------------------------------------------------- 时间边界
@pytest.mark.unit
def test_before_run_after_is_rejected_on_trading_day():
    """交易日但未到运行时间:必须拒绝,且说明原因。

    盘中触发会把未收盘的数据写成当日批次,幂等键一旦占用,
    真正收盘后的重跑就会被自己拦住。
    """
    decision = _decide(
        datetime(2026, 7, 31, 15, 29, 59),
        latest_open=FRIDAY,
        cal_max=FRIDAY,
    )

    assert decision.should_run is False
    assert decision.reason == "before_run_after"
    assert decision.trade_date == FRIDAY
    assert "15:30" in decision.detail


@pytest.mark.unit
def test_exactly_at_run_after_is_allowed():
    """边界取闭区间:等于 run_after 即放行,不要求严格大于。"""
    decision = _decide(
        datetime(2026, 7, 31, 15, 30, 0),
        latest_open=FRIDAY,
        cal_max=FRIDAY,
    )

    assert decision.should_run is True
    assert decision.trade_date == FRIDAY
    assert decision.reason == "ready"


@pytest.mark.unit
def test_after_close_on_trading_day_runs_for_today():
    decision = _decide(
        datetime(2026, 7, 31, 18, 0),
        latest_open=FRIDAY,
        cal_max=FRIDAY,
    )

    assert decision.should_run is True
    assert decision.trade_date == FRIDAY


@pytest.mark.unit
def test_midnight_after_trading_day_still_targets_that_day():
    """跨日:周六凌晨仍应指向周五这一批,而不是凭空造一个周六批次。

    这条同时锁住幂等:周五 18:00 与周六 00:30 两次触发拿到同一个
    trade_date,业务键相同,第二次会被任务表拦下。
    """
    decision = _decide(
        datetime(2026, 8, 1, 0, 30),
        latest_open=FRIDAY,
        cal_max=SATURDAY,
    )

    assert decision.should_run is True
    assert decision.trade_date == FRIDAY


# --------------------------------------------------------------- 交易日判断
@pytest.mark.unit
def test_weekend_targets_previous_open_day_without_time_limit():
    """今天休市:目标日是上一个开市日,且不再受 run_after 约束。

    那一天早就收盘了,再拿今天的钟点去比较毫无意义——周六早上
    9 点触发的补跑不该被"还没到 15:30"挡住。
    """
    decision = _decide(
        datetime(2026, 8, 1, 9, 0),
        latest_open=FRIDAY,
        cal_max=SATURDAY,
    )

    assert decision.should_run is True
    assert decision.trade_date == FRIDAY
    assert decision.reason == "ready"


@pytest.mark.unit
def test_empty_calendar_is_rejected_not_guessed():
    """日历为空:拒绝,不退化成"按周末推断"。

    猜出来的交易日会让整批数据挂在错误日期上,比不跑更糟。
    """
    decision = _decide(
        datetime(2026, 7, 31, 18, 0),
        latest_open=None,
        cal_max=None,
    )

    assert decision.should_run is False
    assert decision.reason == "calendar_missing"
    assert decision.trade_date is None


@pytest.mark.unit
def test_stale_calendar_is_rejected():
    """日历没覆盖到今天:拒绝,并要求回补。

    此时无法判断今天是否开市。若沿用上一个开市日,遇到"今天其实是
    交易日"就会把今天的批次写到昨天的键上,幂等键与真实批次错位。
    """
    decision = _decide(
        datetime(2026, 8, 5, 18, 0),
        latest_open=FRIDAY,
        cal_max=FRIDAY,
    )

    assert decision.should_run is False
    assert decision.reason == "calendar_stale"
    assert decision.trade_date is None
    assert "20260731" in decision.detail


@pytest.mark.unit
def test_calendar_covering_future_is_fine():
    """日历覆盖到未来是正常状态(回补时通常多拉一段),不应误判过期。"""
    decision = _decide(
        datetime(2026, 7, 31, 18, 0),
        latest_open=FRIDAY,
        cal_max="20261231",
    )

    assert decision.should_run is True


@pytest.mark.unit
def test_is_trading_day_membership():
    open_dates = [FRIDAY, "20260730"]

    assert is_trading_day(FRIDAY, open_dates) is True
    assert is_trading_day(SATURDAY, open_dates) is False


# --------------------------------------------------------------- 配置解析
@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("15:30", time(15, 30)), ("1530", time(15, 30)), ("9:05", time(9, 5)), (" 08:00 ", time(8, 0))],
)
def test_parse_run_after_accepts_valid_forms(raw, expected):
    assert parse_run_after(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["1560", "25:00", "下午三点", "", "15:", "abc"])
def test_parse_run_after_rejects_invalid_values(raw):
    """非法运行时间必须抛错,不能静默回退默认值。

    静默兜底会让调度在一个没人预期的时刻触发,而日志里看不出异常。
    """
    with pytest.raises(ScheduleConfigError):
        parse_run_after(raw)


@pytest.mark.unit
def test_missing_schedule_section_means_disabled_not_crash():
    """缺 schedule 段 = 明确关闭,不是崩溃也不是假装在跑。"""
    config = load_schedule_config({"engine": {"default_strategy": "strong_mainup"}})

    assert config.enabled is False
    assert config.strategy == "strong_mainup"
    assert config.run_after_text == "15:30"


@pytest.mark.unit
def test_schedule_strategy_falls_back_to_engine_default():
    config = load_schedule_config(
        {"schedule": {"enabled": True}, "engine": {"default_strategy": "abc"}}
    )

    assert config.enabled is True
    assert config.strategy == "abc"


@pytest.mark.unit
def test_missing_strategy_everywhere_raises():
    """两处都没有策略名时不能猜:扫描策略决定了写进库的是什么。"""
    with pytest.raises(ScheduleConfigError):
        load_schedule_config({"schedule": {"enabled": True}})


@pytest.mark.unit
@pytest.mark.parametrize("tick", [0, -1])
def test_non_positive_tick_raises(tick):
    with pytest.raises(ScheduleConfigError):
        load_schedule_config(
            {"schedule": {"tick_seconds": tick}, "engine": {"default_strategy": "s"}}
        )


@pytest.mark.unit
def test_normalize_trade_date_accepts_dashes():
    assert normalize_trade_date("2026-07-31") == FRIDAY
    assert normalize_trade_date(" 20260731 ") == FRIDAY


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["2026-13-01", "20260732", "abc", "2026073", ""])
def test_normalize_trade_date_rejects_invalid(raw):
    with pytest.raises(ScheduleConfigError):
        normalize_trade_date(raw)
