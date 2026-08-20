"""可见日期闸门:把最近 N 个交易日藏起来,杜绝前视偏差(lookahead bias)。

为什么需要它
------------
选股扫描、Agent 研判、实验四组和收益回填都跑在同一个库上。如果默认以
"本地最新交易日"为基准,补历史批次时就会读到当时还不存在的行情——回测结论
被未来数据污染,而且污染是静默的,看结果根本发现不了。

本模块只回答一个问题:**此刻允许看到的最新交易日是哪天。**

口径
----
- 基准日 base_session:调用方给出的"当前已知最新交易日"。在线链路用
  Tushare 确认的最新完整交易日,离线链路用本地已确认交易日。
- 可见日 visible_as_of:基准日往前退 delay_sessions 个开市日。
- 隐藏窗口 hidden_sessions:比可见日更新的那 delay_sessions 个开市日。
- 请求日 > 可见日 -> 直接拒绝(lookahead_blocked),**不静默改写**成可见日。
  静默改写会让调用方以为自己拿到的是请求的那天,是最坏的一种降级。

纪律
----
- 窗口算不出来(库空/日历缺口/历史不足)就报明确原因,绝不回退成"用最新日"。
- 本模块不写库、不联网,只做纯计算 + 两次只读查询。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# 默认隐藏最近 20 个交易日(约一个自然月),让 T+1~T+10 的收益有完整落地空间。
DEFAULT_DELAY_SESSIONS = 20

# 窗口不可用的原因,用于报错时说清"为什么算不出来"。
REASON_NO_BASE = "no_base_session"
REASON_CALENDAR_MISSING = "calendar_missing_base"
REASON_INSUFFICIENT = "insufficient_sessions"

_REASON_TEXT = {
    REASON_NO_BASE: "本地没有任何日线数据,无法确定基准交易日",
    REASON_CALENDAR_MISSING: "交易日历没有覆盖基准交易日,请先回补 trade_cal",
    REASON_INSUFFICIENT: "交易日历里的历史开市日不足,无法往前退满隐藏窗口",
}


class VisibilityConfigError(ValueError):
    """data.visibility_delay_sessions 配置非法。"""


class LookaheadBlocked(ValueError):
    """请求的交易日落在隐藏窗口内,或窗口本身算不出来。

    code 取值:
    - ``lookahead_blocked``:请求日比可见日更新。
    - ``visibility_window_unavailable``:窗口算不出来,原因见 reason。
    """

    def __init__(self, code: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class VisibilityWindow:
    """一次可见性判定的完整结论。visible_as_of 为 None 表示窗口不可用。"""

    base_session: str | None
    visible_as_of: str | None
    delay_sessions: int
    hidden_sessions: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.visible_as_of is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_session": self.base_session,
            "visible_as_of": self.visible_as_of,
            "delay_sessions": self.delay_sessions,
            "hidden_sessions": list(self.hidden_sessions),
            "hidden_count": len(self.hidden_sessions),
            "reason": self.reason,
        }

    def unavailable_detail(self) -> str:
        return _REASON_TEXT.get(self.reason or "", "可见日期上限不可用")


def load_delay_sessions(settings: dict[str, Any]) -> int:
    """读取 data.visibility_delay_sessions。缺省 20;非法值直接报错不兜底。"""
    data = settings.get("data") or {}
    raw = data.get("visibility_delay_sessions", DEFAULT_DELAY_SESSIONS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise VisibilityConfigError(
            f"data.visibility_delay_sessions 必须是非负整数,实际为 {raw!r}"
        )
    if raw < 0:
        raise VisibilityConfigError(
            f"data.visibility_delay_sessions 必须是非负整数,实际为 {raw}"
        )
    return raw


def build_window(sessions: Sequence[str], delay_sessions: int) -> VisibilityWindow:
    """纯计算:sessions 必须升序,且最后一个就是基准日。

    sessions 至少要有 delay_sessions + 1 个开市日,否则窗口不可用。
    """
    if delay_sessions < 0:
        raise VisibilityConfigError(f"隐藏窗口长度必须非负,实际为 {delay_sessions}")
    ordered = [str(item) for item in sessions]
    if not ordered:
        return VisibilityWindow(None, None, delay_sessions, (), REASON_NO_BASE)
    base = ordered[-1]
    if delay_sessions == 0:
        return VisibilityWindow(base, base, 0, (), None)
    if len(ordered) <= delay_sessions:
        return VisibilityWindow(
            base, None, delay_sessions, tuple(ordered), REASON_INSUFFICIENT
        )
    return VisibilityWindow(
        base,
        ordered[-(delay_sessions + 1)],
        delay_sessions,
        tuple(ordered[-delay_sessions:]),
        None,
    )


def window_for(
    store: Any,
    *,
    exchange: str,
    delay_sessions: int,
    base_session: str | None,
) -> VisibilityWindow:
    """按给定基准日算窗口。只读 trade_cal,不写库、不联网。"""
    if base_session is None:
        return VisibilityWindow(None, None, delay_sessions, (), REASON_NO_BASE)
    base = str(base_session)
    sessions = store.open_dates(exchange, base, delay_sessions + 1)
    if not sessions or sessions[-1] != base:
        return VisibilityWindow(
            base, None, delay_sessions, tuple(sessions), REASON_CALENDAR_MISSING
        )
    return build_window(sessions, delay_sessions)


def local_base_session(store: Any, min_rows: int) -> str | None:
    """本地口径的基准日:已确认交易日优先,小样本回退到最大本地日期。"""
    return store.latest_confirmed_date(min_rows) or store.latest_date()


def resolve_window(
    store: Any,
    settings: dict[str, Any],
    *,
    exchange: str,
    base_session: str | None = None,
) -> VisibilityWindow:
    """按配置 + 基准日算窗口。base_session=None 时用本地口径推。"""
    delay = load_delay_sessions(settings)
    if base_session is None:
        data = settings.get("data") or {}
        min_rows = int(data.get("min_daily_rows") or 0)
        base_session = local_base_session(store, min_rows)
    return window_for(
        store, exchange=exchange, delay_sessions=delay, base_session=base_session
    )


def ensure_visible(requested: str, window: VisibilityWindow) -> str:
    """请求日必须 <= 可见日,否则抛 LookaheadBlocked。返回归一化后的请求日。"""
    if window.visible_as_of is None:
        raise LookaheadBlocked(
            "visibility_window_unavailable",
            window.unavailable_detail(),
            reason=window.reason,
        )
    target = str(requested)
    if target > window.visible_as_of:
        raise LookaheadBlocked(
            "lookahead_blocked",
            f"{target} 落在最近 {window.delay_sessions} 个交易日的隐藏窗口内,"
            f"当前可用的最新交易日是 {window.visible_as_of}",
        )
    return target


def require_visible_as_of(window: VisibilityWindow) -> str:
    """取可见日;不可用直接抛错,绝不回退成基准日。"""
    if window.visible_as_of is None:
        raise LookaheadBlocked(
            "visibility_window_unavailable",
            window.unavailable_detail(),
            reason=window.reason,
        )
    return window.visible_as_of


def backfill_sessions(
    store: Any, *, exchange: str, window: VisibilityWindow, count: int
) -> list[str]:
    """补齐目标日期:以可见日结尾、由旧到新的最多 count 个开市日。"""
    if count <= 0:
        raise ValueError(f"补齐天数必须为正整数,实际为 {count}")
    visible = require_visible_as_of(window)
    return store.open_dates(exchange, visible, count)


__all__ = [
    "DEFAULT_DELAY_SESSIONS",
    "LookaheadBlocked",
    "REASON_CALENDAR_MISSING",
    "REASON_INSUFFICIENT",
    "REASON_NO_BASE",
    "VisibilityConfigError",
    "VisibilityWindow",
    "backfill_sessions",
    "build_window",
    "ensure_visible",
    "load_delay_sessions",
    "local_base_session",
    "require_visible_as_of",
    "resolve_window",
    "window_for",
]
