"""实验四组构造、候选池指纹与买入日涨跌停缺口检查。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from numbers import Integral, Real
from typing import Any

import pandas as pd

from .db_experiments import _DECISION_COLUMNS
from .security import redact_secrets


_REQUIRED_SCAN_COLUMNS = ("ts_code", "name", "industry", "total", "rank")


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
    for value in rows["total"]:
        _finite_number(value, "scan_rows.total")
    for value in rows["rank"]:
        _finite_number(value, "scan_rows.rank")
    return rows


def _json_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    item_method = getattr(value, "item", None)
    if callable(item_method):
        return _json_value(item_method())
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("候选池包含不可序列化的非有限数")
        return value
    if isinstance(value, Real):
        number = float(value)
        if math.isfinite(number):
            return number
        raise ValueError("候选池包含不可序列化的非有限数")
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
        {str(column): _json_value(value) for column, value in row.items()}
        for row in rows.sort_values("ts_code", kind="mergesort").to_dict("records")
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
    maximum_count: int | None = None,
) -> list[dict[str, Any]]:
    raw = agent_result.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"agent_result.{key} 必须是列表")
    if maximum_count is not None and len(raw) > maximum_count:
        raise ValueError(f"agent_result.{key} 不可超过 {maximum_count} 只股票")

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

# 名单语义(用户确认):最终决策人必须给满 N 只,哪怕全部看空——按评分选
# 相对最优,收益对比数据才能持续积累。全看空期的 AI 组代表"相对最优"而非
# "该买",解读收益对比时要记得这一点。



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
    )
    return decision


def build_experiment_decisions(
    run_id: str,
    scan_rows: Any,
    agent_result: Mapping[str, Any] | None,
    final_count: int,
    rule_weight: float = 0.5,
    ai_weight: float = 0.5,
) -> tuple[str, pd.DataFrame]:
    """从冻结候选池生成当前可用的实验组。"""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不可为空")
    if isinstance(final_count, bool) or not isinstance(final_count, Integral) or final_count <= 0:
        raise ValueError("final_count 必须是正整数")
    if agent_result is not None and not isinstance(agent_result, Mapping):
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
    effective_final_count = min(final_count, len(pool))
    pool_hash = candidate_pool_hash(pool)
    pool_by_code = {row["ts_code"]: row for row in pool.to_dict("records")}
    pool_codes = set(pool_by_code)
    final_rows: list[dict[str, Any]] = []
    deep_rows: list[dict[str, Any]] = []
    if agent_result is not None:
        final_rows = _validated_agent_rows(
            agent_result, "final", pool_codes, maximum_count=effective_final_count
        )
        deep_rows = _validated_agent_rows(
            agent_result, "deep", pool_codes, maximum_count=len(pool)
        )
        deep_codes = {item["ts_code"] for item in deep_rows}
        if not {item["ts_code"] for item in final_rows} <= deep_codes:
            raise ValueError("agent_result.final 必须是 deep 的子集")

    decisions: list[dict[str, Any]] = []

    rule_sorted = pool.sort_values(
        ["total", "ts_code"], ascending=[False, True], kind="mergesort"
    ).head(effective_final_count)
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

    # 混合组的 AI 那一半必须来自**辩论评分**，也就是 final 的 score。
    #
    # 原先这里用 deep_rows，而 deep 阶段的 score 就是候选传入的规则分（Pi 侧
    # `deep.push({... score: item.score ...})`，item 来自 coarse），辩论评分只存在于
    # final。结果 ai_percentile 和 rule_percentile 恒等，加权等于没加——线上实测
    # hybrid 三只与 rule 三只完全相同、ai_score == rule_score，混合组退化成规则组副本，
    # 三组对比里有两组是同一个东西。
    #
    # 只有辩成的股票有辩论评分。没辩成的不进混合组：给它们补一个分数就是编造 AI 判断。
    if final_rows:
        hybrid = pd.DataFrame(
            [
                {
                    "ts_code": item["ts_code"],
                    "rule_score": pool_by_code[item["ts_code"]]["total"],
                    "ai_score": item["score"],
                    "agent_item": item,
                }
                for item in final_rows
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
        ).head(effective_final_count)
        for rank, item in enumerate(hybrid.to_dict("records"), start=1):
            row = pool_by_code[item["ts_code"]]
            agent_item = item["agent_item"]
            decision = _base_decision(run_id, "hybrid", row)
            decision.update(
                rank=rank,
                ai_score=float(item["ai_score"]),
                hybrid_score=float(item["hybrid_score"]),
                # 数据源已从 deep 换成 final，理由字段也必须按 final 取
                # （thesis/verdict/action），否则 points/analysts 一个都不存在，理由恒空。
                reason_json=_agent_reason(agent_item, final=True),
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


def required_entry_limit_dates(store: Any, exchange: str = "SSE") -> list[str]:
    """列出已到买入日、但仍缺权威涨跌停覆盖的历史日期。

    一键编排必须在 ``calculate_experiment_returns`` 前调用本函数，并把结果交给
    ``ingest_daily_limits``；否则隔日未运行的实验会一直缺历史涨跌停价，成交状态
    就永远停在 ``pending_entry``。
    """
    rows = store.experiment_entries_awaiting_limits()
    if rows.empty:
        return []
    data_max = store.latest_date()
    if data_max is None:
        return []

    needed: set[str] = set()
    coverage: dict[tuple[str, str], bool] = {}
    entry_dates: dict[str, str | None] = {}
    for _, row in rows.iterrows():
        as_of = row["as_of"]
        if as_of not in entry_dates:
            entry_dates[as_of] = store.sessions_after(exchange, as_of, 1)
        entry_date = entry_dates[as_of]
        if entry_date is None or entry_date > data_max:
            continue
        coverage_key = (row["ts_code"], entry_date)
        if coverage_key not in coverage:
            coverage[coverage_key] = (
                _up_limit(store, row["ts_code"], entry_date) is not None
            )
        if not coverage[coverage_key]:
            needed.add(entry_date)
    return sorted(needed)


def required_entry_bar_codes(
    store: Any, exchange: str = "SSE"
) -> tuple[list[str], str | None]:
    """列出已到买入日、却缺当日日线的股票,以及需要覆盖的最早买入日。

    和 ``required_entry_limit_dates`` 对称,但补的是日线本身而不是涨跌停价。

    本项目每轮扫描只为**当轮候选池**回补日线(见 ``run_scan._backfill_history``),
    全市场截面只覆盖扫描当天。于是更早批次的票在后续买入日整片缺行:实测
    20260721 全市场只入库 1019 行,而前一个交易日有 5524 行。缺行的票会被判成
    ``entry_bar_missing``,收益永远算不出。回填收益前必须先把这些票的日线补上。

    返回 ``(股票列表, 最早买入日)``。无需补采时返回 ``([], None)``。
    """
    rows = store.experiment_entries_awaiting_limits()
    if rows.empty:
        return [], None
    data_max = store.latest_date()
    if data_max is None:
        return [], None

    entry_dates: dict[str, str | None] = {}
    needed: dict[str, str] = {}
    for _, row in rows.iterrows():
        as_of = row["as_of"]
        if as_of not in entry_dates:
            entry_dates[as_of] = store.sessions_after(exchange, as_of, 1)
        entry_date = entry_dates[as_of]
        # 买入日还没走到已入库范围:这是"等未来",补采解决不了,跳过。
        if entry_date is None or entry_date > data_max:
            continue
        ts_code = row["ts_code"]
        if store.close_on(ts_code, entry_date) is not None:
            continue
        # 同一只票可能在多个批次缺行,取最早那个买入日当补采起点。
        previous = needed.get(ts_code)
        if previous is None or entry_date < previous:
            needed[ts_code] = entry_date
    if not needed:
        return [], None
    return sorted(needed), min(needed.values())


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


__all__ = [
    "candidate_pool_hash",
    "build_experiment_decisions",
    "required_entry_bar_codes",
    "required_entry_limit_dates",
]
