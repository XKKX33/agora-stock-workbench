"""Independent T+1 close through T+10 open experiment returns.

可见窗口口径:调用方传入 ``visible_max``(可见日,含当天)后,比它更新的交易日
一律按 ``future_not_visible`` 挂起并且**不读取任何行情**,避免隐藏窗口里的未来
数据渗进收益计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

HORIZONS = (
    "t1_close", "t2_open", "t3_open", "t4_open", "t5_open",
    "t6_open", "t7_open", "t8_open", "t9_open", "t10_open",
)
_LIMIT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ReturnResult:
    """One persisted horizon result, exposed for convenient domain assertions."""

    horizon: str
    gross_return: float | None
    sell_date: str | None
    sell_session: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class ReturnsSummary:
    rows_written: int = 0
    filled: int = 0
    unavailable: int = 0
    pending: int = 0
    results: tuple[ReturnResult, ...] = ()

    def __getitem__(self, horizon: str) -> ReturnResult:
        for result in self.results:
            if result.horizon == horizon:
                return result
        raise KeyError(horizon)

def _valid_price(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _table_exists(store: Any, table: str) -> bool:
    row = store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return row is not None


def _require_return_tables(store: Any) -> None:
    required = ("daily", "daily_limit", "trade_cal")
    missing = [table for table in required if not _table_exists(store, table)]
    if missing:
        raise RuntimeError(f"收益计算缺少关键表: {', '.join(missing)}")


def _bar(store: Any, ts_code: str, trade_date: str):
    return store.con.execute(
        "SELECT open, high, low, close FROM daily WHERE ts_code = ? AND trade_date = ?",
        [ts_code, trade_date],
    ).fetchone()


def _up_limit(store: Any, ts_code: str, trade_date: str):
    row = store.con.execute(
        "SELECT up_limit FROM daily_limit WHERE ts_code = ? AND trade_date = ?",
        [ts_code, trade_date],
    ).fetchone()
    return row[0] if row and _valid_price(row[0]) else None


def _is_locked(bar: tuple[Any, ...], up_limit: float) -> bool:
    return all(
        _valid_price(value)
        and math.isclose(float(value), float(up_limit), rel_tol=0.0, abs_tol=_LIMIT_TOLERANCE)
        for value in bar
    )


def _sessions(store: Any, exchange: str, as_of: str) -> list[str]:
    return [
        row[0]
        for row in store.con.execute(
            """SELECT cal_date FROM trade_cal
               WHERE exchange = ? AND is_open = 1 AND cal_date > ?
               ORDER BY cal_date LIMIT 10""",
            [exchange, as_of],
        ).fetchall()
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _future_status(trade_date: str, data_max: str | None, visible_max: str | None) -> str | None:
    """判定某交易日为什么还算不出收益。先判隐藏、再判数据缺失。

    顺序不能颠倒:隐藏窗口内的日期即使本地已有行情也必须先被拦住,否则
    "本地恰好抓到了未来数据"就会被当成正常可算,前视偏差静默生效。
    """
    if visible_max is not None and trade_date > visible_max:
        return "future_not_visible"
    if data_max is None or trade_date > data_max:
        return "future_not_reached"
    return None


def calculate_experiment_returns(
    store: Any,
    *,
    run_id: str | None = None,
    exchange: str = "SSE",
    visible_max: str | None = None,
) -> ReturnsSummary:
    """Calculate and idempotently persist independent return rows.

    每个 horizon 一行、可单独重试;算不出就留 status/reason,不用 0 冒充。

    ``visible_max`` 为可见日上限(含当天)。比它更新的买入日/卖出日记为
    ``future_not_visible``、计入 pending,且不会去读那天的行情。
    """
    _require_return_tables(store)
    runs = [run_id] if run_id is not None else [
        row[0] for row in store.con.execute(
            "SELECT run_id FROM experiment_runs WHERE status = 'succeeded' ORDER BY run_id"
        ).fetchall()
    ]
    if run_id is not None and store.experiment_run(run_id) is None:
        raise KeyError(f"实验批次不存在: {run_id}")
    data_max_row = store.con.execute("SELECT MAX(trade_date) FROM daily").fetchone()
    data_max = data_max_row[0] if data_max_row else None
    rows: list[dict[str, Any]] = []
    filled = unavailable = pending = 0
    timestamp = _now()
    result_items: list[ReturnResult] = []
    for current_run_id in runs:
        run = store.experiment_run(current_run_id)
        if not run or run.get("status") != "succeeded":
            continue
        decisions = store.con.execute(
            "SELECT group_name, ts_code FROM experiment_decisions WHERE run_id = ? "
            "ORDER BY group_name, ts_code", [current_run_id]
        ).fetchall()
        for group_name, ts_code in decisions:
            sessions = _sessions(store, exchange, run["as_of"])
            entry_date = sessions[0] if sessions else None
            entry_bar = None
            entry_price = None
            entry_status = "filled"
            entry_reason = None
            entry_future = (
                _future_status(entry_date, data_max, visible_max) if entry_date else None
            )
            if entry_date is None:
                entry_status, entry_reason = "calendar_missing", "calendar_missing"
            elif entry_future == "future_not_visible":
                # 隐藏窗口内的买入日:一行行情都不读。
                entry_status, entry_reason = entry_future, "future_not_visible"
            elif entry_future == "future_not_reached":
                entry_status, entry_reason = entry_future, None
            else:
                entry_bar = _bar(store, ts_code, entry_date)
                entry_price = entry_bar[0] if entry_bar else None
                if entry_bar is None:
                    entry_status, entry_reason = "entry_bar_missing", "entry_bar_missing"
                elif not _valid_price(entry_price):
                    entry_status, entry_reason = "entry_bar_missing", "invalid_open"
                else:
                    up_limit = _up_limit(store, ts_code, entry_date)
                    if up_limit is None:
                        entry_status, entry_reason = "pending_entry", "limit_price_missing"
                    elif _is_locked(entry_bar, float(up_limit)):
                        entry_status, entry_reason = "entry_unavailable", "limit_up_locked"
                    else:
                        entry_price = float(entry_price)
            if entry_status != "filled":
                # 没买到就不许留买入价:留着会让"已成交"的判断变成一句空话。
                entry_price = None
            for index, horizon in enumerate(HORIZONS):
                sell_date = sessions[index] if index < len(sessions) else None
                sell_session = "close" if index == 0 else "open"
                status = entry_status
                reason = entry_reason
                sell_price = gross_return = None
                if entry_status == "filled":
                    sell_future = (
                        _future_status(sell_date, data_max, visible_max) if sell_date else None
                    )
                    if sell_date is None:
                        status, reason = "calendar_missing", "calendar_missing"
                    elif sell_future == "future_not_visible":
                        # 隐藏窗口内的卖出日:跳过 _bar,不读那天的价格。
                        status, reason = sell_future, "future_not_visible"
                    elif sell_future == "future_not_reached":
                        status, reason = sell_future, None
                    else:
                        target = _bar(store, ts_code, sell_date)
                        target_price = target[3] if index == 0 and target else target[0] if target else None
                        if not _valid_price(target_price):
                            status, reason = "target_bar_missing", "target_bar_missing"
                        else:
                            sell_price = float(target_price)
                            gross_return = sell_price / float(entry_price) - 1.0
                            status, reason = "filled", None
                result_items.append(ReturnResult(horizon, gross_return, sell_date, sell_session, status, reason))
                if status == "filled":
                    filled += 1
                elif status in {"entry_unavailable", "entry_bar_missing", "target_bar_missing"}:
                    unavailable += 1
                else:
                    pending += 1
                rows.append({
                    "run_id": current_run_id, "group_name": group_name, "ts_code": ts_code,
                    "horizon": horizon, "entry_date": entry_date, "entry_price": entry_price,
                    "sell_date": sell_date, "sell_session": sell_session, "sell_price": sell_price,
                    "status": status, "reason": reason, "gross_return": gross_return,
                    "created_at": timestamp, "updated_at": timestamp,
                })
    if rows:
        store.upsert_experiment_returns(rows)
    return ReturnsSummary(rows_written=len(rows), filled=filled, unavailable=unavailable, pending=pending, results=tuple(result_items))


def returns_summary(
    store: Any,
    *,
    run_id: str | None = None,
    as_of: str | None = None,
    group_name: str | None = None,
    ts_code: str | None = None,
    entry_status: str | None = None,
) -> dict[str, Any]:
    """分组分 horizon 统计,不把算不出的格子当成 0 收益。

    筛选参数和台账列表完全一致,页面上的汇总卡与明细表因此永远同口径。
    """
    rows = store.experiment_returns(
        run_id=run_id,
        as_of=as_of,
        group_name=group_name,
        ts_code=ts_code,
        entry_status=entry_status,
    )
    groups = ("rule", "ai", "hybrid", "benchmark")
    output: dict[str, Any] = {"run_id": run_id, "groups": {}}
    for group in groups:
        group_rows = [row for row in rows if row["group_name"] == group]
        horizons: dict[str, Any] = {}
        for horizon in HORIZONS:
            items = [row for row in group_rows if row["horizon"] == horizon]
            measurable = [row for row in items if row["status"] == "filled" and row["gross_return"] is not None]
            filled = [row for row in items if row["status"] == "filled"]
            unavailable = [row for row in items if row["status"] != "filled"]
            values = sorted(float(row["gross_return"]) for row in measurable)
            average = sum(values) / len(values) if values else None
            median = (values[(len(values)-1)//2] + values[len(values)//2]) / 2 if values else None
            # A portfolio aggregate is only valid when every planned slot is measurable.
            # Keep the partial coverage and status fields for UI explanations instead of
            # silently treating unavailable slots as zero-return cash.
            portfolio = sum(values) / len(items) if items and len(measurable) == len(items) else None
            horizons[horizon] = {
                "planned_count": len(items), "filled_count": len(filled),
                "unavailable_count": len(unavailable), "measurable_count": len(measurable),
                "average": average, "median": median, "portfolio_gross_return": portfolio,
                "coverage": len(measurable) / len(items) if items else None,
                "available": bool(measurable),
                "status_distribution": _status_distribution(items),
                "items": items,
            }
        output["groups"][group] = horizons
    return output


def _status_distribution(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        result[item["status"]] = result.get(item["status"], 0) + 1
    return result


__all__ = ["HORIZONS", "ReturnResult", "ReturnsSummary", "calculate_experiment_returns", "returns_summary"]
