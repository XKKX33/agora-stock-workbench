"""实验四组构造、候选池指纹与真实收益回填。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any

import pandas as pd

from .db_experiments import _DECISION_COLUMNS


_REQUIRED_SCAN_COLUMNS = ("ts_code", "name", "industry", "total", "rank")
_HORIZONS = {"ret1": 1, "ret3": 3, "ret5": 5, "ret10": 10}
_LIMIT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class BackfillSummary:
    """本轮实际发生的回填变化计数。"""

    updated: int = 0
    filled: int = 0
    pending: int = 0
    unavailable: int = 0
    return_filled: int = 0


def _as_frame(rows: Any, label: str) -> pd.DataFrame:
    if isinstance(rows, pd.DataFrame):
        return rows.copy()
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        return pd.DataFrame(list(rows))
    raise ValueError(f"{label} 必须是 DataFrame 或记录列表")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if isinstance(missing, bool) else False


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} 必须是有限数")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数")
    return number


def _validated_scan_rows(scan_rows: Any) -> pd.DataFrame:
    rows = _as_frame(scan_rows, "scan_rows")
    missing_columns = set(_REQUIRED_SCAN_COLUMNS) - set(rows.columns)
    if missing_columns:
        raise ValueError(f"scan_rows 缺少字段: {sorted(missing_columns)}")
    if rows.empty:
        raise ValueError("scan_rows 不可为空")

    for column in ("ts_code", "name", "industry"):
        invalid = rows[column].map(
            lambda value: _is_missing(value)
            or not isinstance(value, str)
            or not value.strip()
        )
        if invalid.any():
            raise ValueError(f"scan_rows.{column} 不可为空")
    if rows["ts_code"].duplicated().any():
        raise ValueError("scan_rows.ts_code 必须唯一")

    rows = rows.reset_index(drop=True)
    rows["total"] = [
        _finite_number(value, "scan_rows.total") for value in rows["total"]
    ]
    rows["rank"] = [
        _finite_number(value, "scan_rows.rank") for value in rows["rank"]
    ]
    return rows


def _json_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (str, int)):
        return value
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("候选池包含不可序列化的非有限数")
        return number
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    item_method = getattr(value, "item", None)
    if callable(item_method):
        return _json_value(item_method())
    raise ValueError(f"候选池包含不可序列化值: {type(value).__name__}")


def _strict_json(value: Any) -> str | None:
    if _is_missing(value) or value == "":
        return None
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("reason_json/risk_json 必须可严格 JSON 序列化") from exc


def candidate_pool_hash(scan_rows: Any) -> str:
    """对冻结候选池的全部字段做顺序无关 SHA-256 指纹。"""
    rows = _validated_scan_rows(scan_rows)
    records = [
        {str(column): _json_value(row[column]) for column in rows.columns}
        for _, row in rows.sort_values("ts_code", kind="mergesort").iterrows()
    ]
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_agent_rows(
    agent_result: Mapping[str, Any],
    key: str,
    pool_codes: set[str],
    *,
    exact_count: int | None = None,
    minimum_count: int | None = None,
) -> list[dict[str, Any]]:
    raw = agent_result.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"agent_result.{key} 必须是列表")
    if exact_count is not None and len(raw) != exact_count:
        raise ValueError(f"agent_result.{key} 必须恰好包含 {exact_count} 只股票")
    if minimum_count is not None and len(raw) < minimum_count:
        raise ValueError(f"agent_result.{key} 必须至少包含 {minimum_count} 只股票")

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"agent_result.{key} 的成员必须是对象")
        code = item.get("ts_code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"agent_result.{key}.ts_code 不可为空")
        if code in seen:
            raise ValueError(f"agent_result.{key} 包含重复 ts_code")
        if code not in pool_codes:
            raise ValueError(f"agent_result.{key} 股票不在候选池: {code}")
        seen.add(code)
        normalized = dict(item)
        normalized["ts_code"] = code
        normalized["score"] = _finite_number(
            item.get("score"), f"agent_result.{key}.score"
        )
        output.append(normalized)
    return output


def _scan_reason(row: Mapping[str, Any]) -> str | None:
    reason: dict[str, Any] = {}
    for field in ("one_line", "gate_reasons_json", "contrib_json"):
        value = row.get(field)
        if _is_missing(value) or value == "":
            continue
        if field.endswith("_json"):
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except json.JSONDecodeError as exc:
                raise ValueError(f"scan_rows.{field} 不是有效 JSON") from exc
        reason[field] = value
    return _strict_json(reason) if reason else None


def _agent_reason(item: Mapping[str, Any], *, final: bool) -> str | None:
    fields = (
        ("thesis", "reason", "action", "verdict", "stance", "debate")
        if final
        else ("points", "stance", "scores", "analysts")
    )
    reason = {
        field: item[field]
        for field in fields
        if field in item and not _is_missing(item[field]) and item[field] not in ("", [], {})
    }
    return _strict_json(reason) if reason else None


def _base_decision(run_id: str, group_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    decision = {column: None for column in _DECISION_COLUMNS}
    decision.update(
        run_id=run_id,
        group_name=group_name,
        ts_code=row["ts_code"],
        name=row["name"],
        industry=row["industry"],
        rule_score=float(row["total"]),
        entry_status="pending_entry",
    )
    for horizon in _HORIZONS:
        decision[f"{horizon}_status"] = "future_not_reached"
    return decision


def build_experiment_decisions(
    run_id: str,
    scan_rows: Any,
    agent_result: Mapping[str, Any],
    final_count: int,
    rule_weight: float = 0.5,
    ai_weight: float = 0.5,
) -> tuple[str, pd.DataFrame]:
    """从同一个冻结候选池生成 rule/ai/hybrid/benchmark 四组明细。"""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不可为空")
    if isinstance(final_count, bool) or not isinstance(final_count, Integral) or final_count <= 0:
        raise ValueError("final_count 必须是正整数")
    if not isinstance(agent_result, Mapping):
        raise ValueError("agent_result 必须是对象")

    rule_weight_value = _finite_number(rule_weight, "混合权重")
    ai_weight_value = _finite_number(ai_weight, "混合权重")
    if not 0 <= rule_weight_value <= 1 or not 0 <= ai_weight_value <= 1:
        raise ValueError("混合权重必须在 0 到 1 之间")
    if not math.isclose(
        rule_weight_value + ai_weight_value, 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("混合权重之和必须为 1")

    pool = _validated_scan_rows(scan_rows)
    if len(pool) < final_count:
        raise ValueError("冻结候选池数量不可少于 final_count")
    pool_hash = candidate_pool_hash(pool)
    pool_by_code = {row["ts_code"]: row for row in pool.to_dict("records")}
    pool_codes = set(pool_by_code)
    final_rows = _validated_agent_rows(
        agent_result, "final", pool_codes, exact_count=final_count
    )
    deep_rows = _validated_agent_rows(
        agent_result, "deep", pool_codes, minimum_count=final_count
    )

    decisions: list[dict[str, Any]] = []

    rule_sorted = pool.sort_values(
        ["total", "ts_code"], ascending=[False, True], kind="mergesort"
    ).head(final_count)
    for rank, row in enumerate(rule_sorted.to_dict("records"), start=1):
        decision = _base_decision(run_id, "rule", row)
        decision.update(rank=rank, reason_json=_scan_reason(row))
        decisions.append(decision)

    final_sorted = sorted(final_rows, key=lambda item: (-item["score"], item["ts_code"]))
    for rank, item in enumerate(final_sorted, start=1):
        row = pool_by_code[item["ts_code"]]
        decision = _base_decision(run_id, "ai", row)
        decision.update(
            rank=rank,
            ai_score=item["score"],
            reason_json=_agent_reason(item, final=True),
            risk_json=_strict_json(item.get("risks")),
        )
        decisions.append(decision)

    hybrid = pd.DataFrame(
        [
            {
                "ts_code": item["ts_code"],
                "rule_score": pool_by_code[item["ts_code"]]["total"],
                "ai_score": item["score"],
                "agent_item": item,
            }
            for item in deep_rows
        ]
    )
    hybrid["rule_percentile"] = hybrid["rule_score"].rank(
        method="average", pct=True
    )
    hybrid["ai_percentile"] = hybrid["ai_score"].rank(
        method="average", pct=True
    )
    hybrid["hybrid_score"] = (
        rule_weight_value * hybrid["rule_percentile"]
        + ai_weight_value * hybrid["ai_percentile"]
    )
    hybrid = hybrid.sort_values(
        ["hybrid_score", "ts_code"], ascending=[False, True], kind="mergesort"
    ).head(final_count)
    for rank, item in enumerate(hybrid.to_dict("records"), start=1):
        row = pool_by_code[item["ts_code"]]
        agent_item = item["agent_item"]
        decision = _base_decision(run_id, "hybrid", row)
        decision.update(
            rank=rank,
            ai_score=float(item["ai_score"]),
            hybrid_score=float(item["hybrid_score"]),
            reason_json=_agent_reason(agent_item, final=False),
            risk_json=_strict_json(agent_item.get("risks")),
        )
        decisions.append(decision)

    benchmark = pool.sort_values(
        ["total", "ts_code"], ascending=[False, True], kind="mergesort"
    )
    for rank, row in enumerate(benchmark.to_dict("records"), start=1):
        decision = _base_decision(run_id, "benchmark", row)
        decision.update(rank=rank, reason_json=_scan_reason(row))
        decisions.append(decision)

    return pool_hash, pd.DataFrame(decisions, columns=_DECISION_COLUMNS)


def _pending_experiment_rows(store: Any) -> pd.DataFrame:
    columns = ", ".join(f"d.{column}" for column in _DECISION_COLUMNS)
    return store.con.execute(
        f"""
        SELECT {columns}, r.as_of
        FROM experiment_decisions d
        JOIN experiment_runs r ON r.run_id = d.run_id
        WHERE r.status = ?
          AND (
              d.entry_status IS NULL
              OR d.entry_status = 'pending_entry'
              OR (
                  d.entry_status = 'filled'
                  AND (
                      d.ret1_status IS NULL OR d.ret1_status <> 'filled'
                      OR d.ret3_status IS NULL OR d.ret3_status <> 'filled'
                      OR d.ret5_status IS NULL OR d.ret5_status <> 'filled'
                      OR d.ret10_status IS NULL OR d.ret10_status <> 'filled'
                  )
              )
          )
        ORDER BY d.run_id, d.group_name, d.ts_code
        """,
        ["succeeded"],
    ).df()


def _sessions_after(store: Any, exchange: str, as_of: str) -> list[str]:
    return [
        row[0]
        for row in store.con.execute(
            """
            SELECT cal_date FROM trade_cal
            WHERE exchange = ? AND is_open = 1 AND cal_date > ?
            ORDER BY cal_date ASC LIMIT 10
            """,
            [exchange, as_of],
        ).fetchall()
    ]


def _daily_bar(store: Any, ts_code: str, trade_date: str) -> tuple[Any, ...] | None:
    return store.con.execute(
        """
        SELECT open, high, low, close FROM daily
        WHERE ts_code = ? AND trade_date = ?
        """,
        [ts_code, trade_date],
    ).fetchone()


def _up_limit(store: Any, ts_code: str, trade_date: str) -> float | None:
    row = store.con.execute(
        """
        SELECT up_limit FROM daily_limit
        WHERE ts_code = ? AND trade_date = ?
        """,
        [ts_code, trade_date],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _valid_price(value: Any) -> bool:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0


def _unavailable_fields(reason: str, sessions: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "entry_price": None,
        "entry_status": "entry_unavailable",
        "entry_reason": reason,
    }
    for name, number in _HORIZONS.items():
        fields[name] = None
        fields[f"{name}_target_date"] = (
            sessions[number - 1] if len(sessions) >= number else None
        )
        fields[f"{name}_status"] = "entry_unavailable"
        fields[f"{name}_reason"] = reason
    return fields


def _pending_entry_fields(
    reason: str, sessions: list[str], data_max: str | None
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "entry_price": None,
        "entry_status": "pending_entry",
        "entry_reason": reason,
    }
    for name, number in _HORIZONS.items():
        target = sessions[number - 1] if len(sessions) >= number else None
        fields[name] = None
        fields[f"{name}_target_date"] = target
        if target is None:
            fields[f"{name}_status"] = "calendar_missing"
            fields[f"{name}_reason"] = "calendar_missing"
        elif data_max is None or target > data_max:
            fields[f"{name}_status"] = "future_not_reached"
            fields[f"{name}_reason"] = None
    return fields


def _current_value(value: Any) -> Any:
    return None if _is_missing(value) else value


def _different(current: Any, desired: Any) -> bool:
    left = _current_value(current)
    right = _current_value(desired)
    if left is None or right is None:
        return left is not right
    return left != right


def backfill_experiment_returns(store: Any, exchange: str = "SSE") -> BackfillSummary:
    """按信号日后的市场交易日和同一个次日开盘价回填实验收益。"""
    pending_rows = _pending_experiment_rows(store)
    if pending_rows.empty:
        return BackfillSummary()

    data_max = store.latest_date()
    updated = filled = pending = unavailable = return_filled = 0

    for _, row in pending_rows.iterrows():
        ts_code = row["ts_code"]
        sessions = _sessions_after(store, exchange, row["as_of"])
        entry_date = sessions[0] if sessions else None
        desired: dict[str, Any] = {"entry_date": entry_date}

        if entry_date is None:
            desired.update(_pending_entry_fields("calendar_missing", sessions, data_max))
        elif data_max is None or entry_date > data_max:
            desired.update(_pending_entry_fields("future_not_reached", sessions, data_max))
        else:
            bar = _daily_bar(store, ts_code, entry_date)
            if bar is None:
                desired.update(_unavailable_fields("entry_bar_missing", sessions))
            elif not _valid_price(bar[0]):
                desired.update(_unavailable_fields("invalid_open", sessions))
            else:
                up_limit = _up_limit(store, ts_code, entry_date)
                if up_limit is None:
                    # 涨停价以后补齐后必须能重试，因此不把收益状态改成终态。
                    desired.update(
                        entry_price=None,
                        entry_status="pending_entry",
                        entry_reason="limit_price_missing",
                    )
                elif all(
                    _valid_price(price)
                    and math.isclose(
                        float(price), up_limit, rel_tol=0.0, abs_tol=_LIMIT_TOLERANCE
                    )
                    for price in bar
                ):
                    desired.update(_unavailable_fields("limit_up_locked", sessions))
                else:
                    entry_price = float(bar[0])
                    desired.update(
                        entry_price=entry_price,
                        entry_status="filled",
                        entry_reason=None,
                    )
                    for name, number in _HORIZONS.items():
                        target = sessions[number - 1] if len(sessions) >= number else None
                        desired[name] = None
                        desired[f"{name}_target_date"] = target
                        if target is None:
                            desired[f"{name}_status"] = "calendar_missing"
                            desired[f"{name}_reason"] = "calendar_missing"
                        elif data_max is None or target > data_max:
                            desired[f"{name}_status"] = "future_not_reached"
                            desired[f"{name}_reason"] = None
                        else:
                            target_bar = _daily_bar(store, ts_code, target)
                            close = target_bar[3] if target_bar is not None else None
                            if not _valid_price(close):
                                desired[f"{name}_status"] = "target_bar_missing"
                                desired[f"{name}_reason"] = "target_bar_missing"
                            else:
                                desired[name] = float(close) / entry_price - 1.0
                                desired[f"{name}_status"] = "filled"
                                desired[f"{name}_reason"] = None

        changes = {
            field: value
            for field, value in desired.items()
            if _different(row.get(field), value)
        }
        if not changes:
            continue

        old_entry_status = _current_value(row.get("entry_status"))
        new_entry_status = desired.get("entry_status", old_entry_status)
        if new_entry_status == "filled" and old_entry_status != "filled":
            filled += 1
        elif new_entry_status == "pending_entry":
            pending += 1
        elif new_entry_status == "entry_unavailable" and old_entry_status != "entry_unavailable":
            unavailable += 1
        for name in _HORIZONS:
            if (
                desired.get(f"{name}_status") == "filled"
                and _current_value(row.get(f"{name}_status")) != "filled"
            ):
                return_filled += 1

        store.update_experiment_decision(
            row["run_id"], row["group_name"], ts_code, **changes
        )
        updated += 1

    return BackfillSummary(
        updated=updated,
        filled=filled,
        pending=pending,
        unavailable=unavailable,
        return_filled=return_filled,
    )


__all__ = [
    "BackfillSummary",
    "candidate_pool_hash",
    "build_experiment_decisions",
    "backfill_experiment_returns",
]
