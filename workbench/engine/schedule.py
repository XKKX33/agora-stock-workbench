"""收盘后调度的交易日闸门。

这里只回答一个问题:**此刻是否应该为某个交易日启动盘后任务链,如果不该,为什么**。

设计要点:

1. 闸门是纯函数。日历、当前时间都从参数传入,不在函数里读库读钟。
   原因:交易日判定和时间边界是本阶段最需要测试的逻辑(节假日、周末、
   收盘前、跨日、日历过期),纯函数才能用固定时钟穷举这些分支。
2. 闸门**只判交易日与运行时间**,不判数据是否已收盘确认。
   数据确认必须放在任务链内部、行情摄取之后——摄取本身就是产出数据的步骤,
   在它之前要求"数据已确认"会让在线模式永远无法启动。
3. 目标交易日取自 `trade_cal`,不取自本地行情的最大日期。
   日历是权威口径:本地行情落后时,目标日仍应是真实的最近交易日,
   这样幂等键指向的批次才稳定,不会因为数据落后而漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional, Sequence

# 允许 "15:30" 与 "1530" 两种写法
_RUN_AFTER_RE = re.compile(r"^(\d{1,2}):?(\d{2})$")

DATE_FMT = "%Y%m%d"


class ScheduleConfigError(ValueError):
    """调度配置非法。配置错误必须在启动时暴露,不做默认值兜底。"""


@dataclass(frozen=True)
class ScheduleConfig:
    """盘后调度配置。字段全部显式,不接受隐式默认的时间。"""

    enabled: bool
    run_after: time
    exchange: str
    strategy: str
    online: bool
    tick_seconds: int

    @property
    def run_after_text(self) -> str:
        return self.run_after.strftime("%H:%M")


@dataclass(frozen=True)
class GateDecision:
    """闸门结论。

    should_run=False 时 reason 必须能直接展示给用户,说明为什么没跑,
    而不是让调用方面对一个沉默的 None。
    """

    should_run: bool
    trade_date: Optional[str]
    reason: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "should_run": self.should_run,
            "trade_date": self.trade_date,
            "reason": self.reason,
            "detail": self.detail,
        }


def parse_run_after(raw: object) -> time:
    """把配置里的运行时间解析为 time。非法值直接抛,不回退默认值。

    不做兜底的理由:`run_after` 写错(例如 "1560" 或 "下午三点")时静默回退到
    某个默认时间,会让调度在一个没人预期的时刻触发,而日志里看不出异常。
    """
    if isinstance(raw, time):
        return raw
    text = str(raw).strip()
    match = _RUN_AFTER_RE.match(text)
    if not match:
        raise ScheduleConfigError(
            f"schedule.run_after 格式非法: {raw!r},应为 HH:MM(例如 15:30)"
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleConfigError(f"schedule.run_after 超出合法时间范围: {raw!r}")
    return time(hour=hour, minute=minute)


def load_schedule_config(settings: dict) -> ScheduleConfig:
    """从 settings.yaml 的 schedule 段构造配置。

    缺少 schedule 段时按"未启用"处理:这不是降级,而是明确的关闭状态,
    调度器会照常上报 enabled=False,不会假装在运行。
    """
    raw = (settings or {}).get("schedule") or {}
    engine_cfg = (settings or {}).get("engine") or {}

    tick = int(raw.get("tick_seconds", 60))
    if tick <= 0:
        raise ScheduleConfigError(f"schedule.tick_seconds 必须为正整数,收到 {tick!r}")

    strategy = raw.get("strategy") or engine_cfg.get("default_strategy")
    if not strategy:
        raise ScheduleConfigError(
            "schedule.strategy 与 engine.default_strategy 均未配置,无法确定盘后扫描策略"
        )

    return ScheduleConfig(
        enabled=bool(raw.get("enabled", False)),
        run_after=parse_run_after(raw.get("run_after", "15:30")),
        exchange=str(raw.get("exchange", "SSE")),
        strategy=str(strategy),
        online=bool(raw.get("online", False)),
        tick_seconds=tick,
    )


def decide_due_run(
    *,
    now: datetime,
    run_after: time,
    latest_open_date: Optional[str],
    calendar_max: Optional[str],
) -> GateDecision:
    """判断此刻是否应为某交易日启动盘后任务链(纯函数)。

    参数:
        now: 当前本地时间。
        run_after: 收盘后允许运行的时刻(当日)。
        latest_open_date: 日历中 <= 今天的最近开市日(YYYYMMDD),无则 None。
        calendar_max: 日历覆盖到的最大日期(YYYYMMDD),无则 None。

    判定顺序及理由:

    1. 日历为空 -> 无法判交易日,拒绝运行。绝不退化成"按周末判断",
       猜出来的交易日会让整批数据挂在错误的日期上。
    2. 日历没覆盖到今天 -> 拒绝运行。日历过期时无法知道今天是否开市,
       若沿用上一个开市日,遇到"今天其实是交易日"就会把今天的盘后任务
       写到昨天的键上,幂等键与真实批次错位。
    3. 最近开市日就是今天 -> 必须 now >= run_after,否则还没收盘。
    4. 最近开市日早于今天(今天休市) -> 那一天早已收盘,时间闸门自然满足。
    """
    if not latest_open_date:
        return GateDecision(
            should_run=False,
            trade_date=None,
            reason="calendar_missing",
            detail="trade_cal 中没有可用的开市日记录,无法判定交易日",
        )
    if not calendar_max:
        return GateDecision(
            should_run=False,
            trade_date=None,
            reason="calendar_missing",
            detail="trade_cal 为空,无法判定交易日",
        )

    today_text = now.strftime(DATE_FMT)
    if calendar_max < today_text:
        return GateDecision(
            should_run=False,
            trade_date=None,
            reason="calendar_stale",
            detail=(
                f"交易日历只覆盖到 {calendar_max},未覆盖今天 {today_text},"
                "无法判定今天是否开市;请先回补 trade_cal"
            ),
        )

    if latest_open_date == today_text and now.time() < run_after:
        return GateDecision(
            should_run=False,
            trade_date=latest_open_date,
            reason="before_run_after",
            detail=(
                f"今天 {today_text} 是交易日,但当前 {now.strftime('%H:%M')} "
                f"早于配置的运行时间 {run_after.strftime('%H:%M')}"
            ),
        )

    return GateDecision(
        should_run=True,
        trade_date=latest_open_date,
        reason="ready",
        detail=f"目标交易日 {latest_open_date} 已收盘,可执行盘后任务链",
    )


def is_trading_day(target: str, open_dates: Sequence[str]) -> bool:
    """target 是否为开市日。open_dates 需为日历中 is_open=1 的日期集合。"""
    return target in set(open_dates)


def normalize_trade_date(raw: str) -> str:
    """校验并规范手动指定的交易日。非法格式直接抛,不猜测用户意图。"""
    text = str(raw).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ScheduleConfigError(f"交易日格式非法: {raw!r},应为 YYYYMMDD")
    try:
        date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError as exc:
        raise ScheduleConfigError(f"交易日不是合法日期: {raw!r}") from exc
    return text
