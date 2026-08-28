"""扫描入口:ingest → universe → context → score → 台账。

编排全链路,是旧脚本 under70_strict_mainup_scan.py 的引擎化替身:
1. 确认最新收盘交易日(本地优先,缺失则从 Tushare 拉齐)。
2. 构建候选池(硬过滤 + 行业热度 + 种子召回)。
3. 为每只候选构建 StockContext(含资金后置确认)。
4. 打分 + 门槛 + 行业去重 + 取 top_n。
5. 写 picks 台账(供事后复盘/IC 自检)。

前视纪律:所有历史、资金流查询按 <= as_of 过滤;资金流缺失置 NaN,不反填未来。
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
import pandas as pd

from .config import load_settings, load_strategy, resolve_path, tushare_token


def _stable_json(value: Any) -> str:
    """Canonical JSON used for reproducible strategy batch identities."""
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): normalize(v) for k, v in sorted(item.items(), key=lambda pair: str(pair[0]))}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, float) and math.isnan(item):
            return None
        if hasattr(item, "item"):
            return normalize(item.item())
        return item
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _config_hash(strategy: Dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(strategy).encode("utf-8")).hexdigest()


def _candidate_hash(candidates: pd.DataFrame) -> str:
    if candidates is None or candidates.empty:
        payload: list[dict[str, Any]] = []
    else:
        frame = candidates.copy()
        if "ts_code" in frame.columns:
            frame = frame.sort_values("ts_code", kind="stable")
        payload = frame.to_dict(orient="records")
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

from .db import Store
from .factors.context import build_context, StockContext
from .factors.money import classify_money
from .ingest_tushare import (
    TushareClient,
    confirm_latest_trade_date,
    ingest_calendar,
    ingest_history,
    ingest_moneyflow,
    ingest_snapshot,
)
from .score import dedup_and_top, score_pool, ScoredStock
from .universe import (
    amount_top15pct_threshold,
    apply_universe,
    build_candidates,
    industry_heat,
    industry_meta,
)


# 日历前瞻的假期余量(自然日):A 股最长连休(春节/国庆)约 10 天,取 14 天留空间。
_HOLIDAY_BUFFER_DAYS = 14

# 交易日历口径:全项目统一按上交所日历推可见窗口与回补区间。
EXCHANGE = "SSE"


@dataclass
class ScanResult:
    run_id: str
    as_of: str
    strategy: str
    config_hash: str
    candidate_hash: str
    data_cutoff_at: Optional[str]
    candidate_count: int
    scored_count: int
    passed_count: int
    final: List[ScoredStock]
    scored: List[ScoredStock] = field(default_factory=list)
    top_industries: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class ScanPreparation:
    """评分前已经冻结的数据与策略输入。"""

    db_path: str
    as_of: str
    strategy_name: str
    strategy: Dict[str, Any]
    online: bool
    candidates: pd.DataFrame
    contexts: List[StockContext]
    top_industries: List[Dict[str, Any]]
    snapshot_count: int
    data_cutoff_at: Optional[str] = None
    data_quality: Dict[str, Any] = field(default_factory=dict)
    minimum_daily_rows: int = 0


_REQUIRED_CANDIDATE_FIELDS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol",
    "amount",
    "symbol",
    "name",
    "industry",
    "turnover_rate",
    "total_mv",
    "circ_mv",
)
_NUMERIC_CANDIDATE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol",
    "amount",
    "turnover_rate",
    "total_mv",
    "circ_mv",
)
_OPTIONAL_NUMERIC_CANDIDATE_FIELDS = ("volume_ratio",)


def _make_client(settings: Dict[str, Any]) -> TushareClient:
    """构造 Tushare 客户端(延迟导入 tushare,避免无 token 环境导入失败)。"""
    token = tushare_token(settings)
    ts_cfg = settings.get("tushare", {}) or {}
    return TushareClient(
        token,
        retry=int(ts_cfg.get("retry", 3)),
        sleep=float(ts_cfg.get("sleep", 0.02)),
        timeout=int(ts_cfg.get("request_timeout_seconds", 120)),
    )


def _calendar_lookahead_end() -> str:
    """日历要拉到的末日:今天 + 覆盖最长回填期限所需的余量。

    期限从 postmortem.HORIZONS 取,避免这里写死天数后与回填口径脱钩。
    HORIZONS 是"未来第 N 个开市日",换算成自然日按 2 倍粗放折算(周末),
    再加一段假期余量(春节/国庆连休最长约 10 天)。宁可多拉——日历多几天
    只是多几行 trade_cal,而少拉会让未到期样本被误判成缺数据。
    """
    from .postmortem import HORIZONS

    max_sessions = max(HORIZONS.values())
    days_ahead = max_sessions * 2 + _HOLIDAY_BUFFER_DAYS
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y%m%d")


def _ensure_data(
    store: Store,
    settings: Dict[str, Any],
    *,
    online: bool,
    client: Optional[TushareClient],
) -> tuple[str, int, Dict[str, int]]:
    """确认 as_of；在线时补齐截面、基础数据和日历。"""
    min_rows = int(settings["data"]["min_daily_rows"])
    local = store.latest_confirmed_date(min_rows)
    if not online:
        # 离线优先用已确认交易日;小样本(测试/回补中)回退到最大本地日期
        as_of = local or store.latest_date()
        if not as_of:
            raise RuntimeError("离线模式但本地无任何日线数据,请先联网 ingest。")
        rows = int(
            store.con.execute(
                "SELECT COUNT(*) FROM daily WHERE trade_date = ?", [as_of]
            ).fetchone()[0]
        )
        return as_of, rows, {}

    assert client is not None
    as_of, rows = confirm_latest_trade_date(client, min_rows)
    start_cal = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    # 日历要往前多拉一段,不能止于 as_of。回填 T+N 收益需要知道"as_of 之后
    # 第 N 个开市日是哪天",而日历末日等于 as_of 时 sessions_after 永远返回
    # None,于是"还在等未来"会被误判成"日历该回补了"(calendar_missing 进
    # needs_attention,被当成要人处理的缺数据)。往前取到覆盖最长期限之后,
    # 未到期的样本才会正确落到 future_not_reached。
    ingest_calendar(store, client, start_cal, _calendar_lookahead_end())
    ingested = ingest_snapshot(store, client, as_of)
    return as_of, rows, ingested


def _backfill_history(
    store: Store,
    client: TushareClient,
    cand: pd.DataFrame,
    as_of: str,
    bars: int,
) -> int:
    """为候选池回补足量日线(<= as_of),已有足量则跳过。"""
    open_dates = store.open_dates(EXCHANGE, as_of, bars + 20)
    start = open_dates[0] if open_dates else (
        (datetime.now() - timedelta(days=bars * 2)).strftime("%Y%m%d")
    )
    need: List[str] = []
    for code in cand["ts_code"].tolist():
        have = store.history(code, as_of, bars)
        if have is None or len(have) < bars:
            need.append(code)
    if need:
        return ingest_history(store, client, need, start, as_of)
    return 0


def _partition_candidates_by_history(
    store: Store,
    candidates: pd.DataFrame,
    as_of: str,
    required_bars: int,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """按统一历史窗口筛出可评分股票，并保留完整排除记录。"""
    counts = [
        {
            "ts_code": str(code),
            "bars": len(store.history(str(code), as_of, required_bars)),
        }
        for code in candidates["ts_code"].astype(str).drop_duplicates()
    ]
    excluded = [item for item in counts if item["bars"] < required_bars]
    eligible_codes = {
        item["ts_code"] for item in counts if item["bars"] >= required_bars
    }
    eligible = candidates.loc[
        candidates["ts_code"].astype(str).isin(eligible_codes)
    ].copy()
    eligible.reset_index(drop=True, inplace=True)
    history_window = {
        "required_bars": int(required_bars),
        "evaluated_count": len(counts),
        "eligible_count": len(eligible_codes),
        "min_bars": min((item["bars"] for item in counts), default=0),
        "satisfied": bool(counts) and not excluded,
        "missing_count": len(excluded),
        "excluded_count": len(excluded),
        "excluded": excluded,
    }
    return eligible, history_window


def _backfill_moneyflow(
    store: Store,
    client: TushareClient,
    codes: List[str],
    as_of: str,
) -> int:
    expected_by_code: Dict[str, set[str]] = {}
    starts: List[str] = []
    need: List[str] = []
    for code in codes:
        history = store.history(code, as_of, 5)
        expected = set(history["trade_date"].astype(str)) if not history.empty else set()
        expected_by_code[code] = expected
        starts.extend(expected)
        have = store.moneyflow_tail(code, as_of, 5)
        actual = (
            set(have["trade_date"].astype(str))
            if have is not None and not have.empty
            else set()
        )
        if not expected or not expected.issubset(actual):
            need.append(code)
    if need:
        if not starts:
            raise RuntimeError("候选池缺少资金流回补所需的日线日期")
        return ingest_moneyflow(store, client, need, min(starts), as_of)
    return 0


def _money_class_for(
    store: Store, code: str, as_of: str
) -> tuple[Optional[str], Dict[str, float]]:
    """由本地 moneyflow 计算近5日资金分层。缺失返回 (None, {})。"""
    mf = store.moneyflow_tail(code, as_of, 5)
    if mf is None or mf.empty:
        return None, {}
    net5 = (
        float(mf["net_mf_amount"].astype(float).sum())
        if "net_mf_amount" in mf.columns
        else float("nan")
    )
    big_daily = (
        (mf["buy_lg_amount"].astype(float) - mf["sell_lg_amount"].astype(float))
        + (mf["buy_elg_amount"].astype(float) - mf["sell_elg_amount"].astype(float))
    )
    big5 = float(big_daily.sum())
    cls = classify_money(net5, big5)
    money = {
        "net5": net5,
        "big5": big5,
        "big_pos_days": float((big_daily > 0).sum()),
    }
    return cls, money


def _build_contexts(
    store: Store,
    cand: pd.DataFrame,
    heat_map: Dict[str, float],
    rank_map: Dict[str, int],
    top_inds: List[str],
    as_of: str,
    bars: int,
    price_max: Optional[float],
) -> List[StockContext]:
    amount_q85 = amount_top15pct_threshold(cand) if len(cand) else 0.0
    top8 = set(top_inds[:8])

    contexts: List[StockContext] = []
    for _, r in cand.iterrows():
        code = r["ts_code"]
        hist = store.history(code, as_of, bars)
        snap = {
            "pct_chg": r.get("pct_chg"),
            "amount": r.get("amount"),
            "turnover_rate": r.get("turnover_rate"),
            "volume_ratio": r.get("volume_ratio"),
        }
        industry = r.get("industry")
        ctx = build_context(
            ts_code=code,
            name=r.get("name"),
            industry=industry,
            as_of=as_of,
            hist=hist,
            snapshot=snap,
            industry_heat=float(heat_map.get(industry, 0.0)),
            industry_rank=int(rank_map.get(industry, 999)),
            amount_top15pct=bool(float(r.get("amount", 0) or 0) > amount_q85),
            min_bars=bars,
        )
        if ctx is None:
            continue
        # 单价上限二次确认(用确认收盘价)
        if price_max is not None and ctx.get("last_close") >= float(price_max):
            continue
        ctx.feat["industry_top8"] = 1.0 if industry in top8 else 0.0
        # 资金后置确认
        cls, money = _money_class_for(store, code, as_of)
        ctx.money_class = cls
        ctx.money = money
        for k, v in money.items():
            ctx.feat[k] = v
        contexts.append(ctx)
    return contexts


def _coverage(matched: int, expected: int) -> Optional[float]:
    if expected <= 0:
        return None
    return float(matched) / float(expected)


def _candidate_missing_values(candidates: pd.DataFrame) -> int:
    missing = 0
    for field_name in _REQUIRED_CANDIDATE_FIELDS:
        if field_name not in candidates.columns:
            missing += len(candidates)
            continue
        values = candidates[field_name]
        invalid = values.isna()
        if field_name in _NUMERIC_CANDIDATE_FIELDS:
            numeric = pd.to_numeric(values, errors="coerce")
            invalid = invalid | numeric.isna() | ~numeric.map(math.isfinite)
        else:
            invalid = invalid | values.map(lambda value: not str(value).strip())
        missing += int(invalid.sum())
    return missing


def _optional_candidate_field_quality(candidates: pd.DataFrame) -> Dict[str, Any]:
    quality: Dict[str, Any] = {}
    for field_name in _OPTIONAL_NUMERIC_CANDIDATE_FIELDS:
        if field_name not in candidates.columns:
            quality[field_name] = {
                "missing_count": len(candidates),
                "missing_rate": 1.0 if len(candidates) else None,
                "missing_sample": candidates["ts_code"].astype(str).head(20).tolist(),
                "invalid_count": 0,
            }
            continue
        raw = candidates[field_name]
        numeric = pd.to_numeric(raw, errors="coerce")
        missing = raw.isna()
        invalid = (~missing) & (
            numeric.isna() | ~numeric.map(math.isfinite) | numeric.lt(0)
        )
        quality[field_name] = {
            "missing_count": int(missing.sum()),
            "missing_rate": _coverage(int(missing.sum()), len(candidates)),
            "missing_sample": candidates.loc[missing, "ts_code"].astype(str).head(20).tolist(),
            "invalid_count": int(invalid.sum()),
        }
    return quality


def _build_data_quality(
    store: Store,
    *,
    as_of: str,
    online: bool,
    candidates: pd.DataFrame,
    contexts: List[StockContext],
    minimum_daily_rows: int,
    expected_daily_rows: Optional[int],
    ingested_rows: Dict[str, int],
    history_window: Dict[str, Any],
    context_filter: Dict[str, Any],
) -> Dict[str, Any]:
    """冻结评分输入对应的数据来源、日期、行数和精确覆盖率。"""
    source = "tushare" if online else "local_database"
    candidate_codes = candidates[["ts_code"]].drop_duplicates().copy()
    candidate_count = int(len(candidate_codes))
    relation = "_scan_quality_codes"
    store.con.register(relation, candidate_codes)
    try:
        daily_rows = int(
            store.con.execute(
                "SELECT COUNT(*) FROM daily WHERE trade_date = ?", [as_of]
            ).fetchone()[0]
        )
        listed_codes = {
            str(row[0])
            for row in store.con.execute("SELECT ts_code FROM stock_basic").fetchall()
        }
        lifecycle_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT ts_code FROM security_lifecycle "
                "WHERE list_date IS NOT NULL AND list_date <= ? "
                "AND (delist_date IS NULL OR delist_date = '' OR delist_date >= ?)",
                [as_of, as_of],
            ).fetchall()
        }
        previously_traded_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT DISTINCT ts_code FROM daily WHERE trade_date < ?",
                [as_of],
            ).fetchall()
        }
        suspended_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT ts_code FROM suspend_daily WHERE trade_date = ?", [as_of]
            ).fetchall()
        }
        stock_basic_rows = len(listed_codes)
        daily_basic_rows = int(
            store.con.execute(
                "SELECT COUNT(*) FROM daily_basic WHERE trade_date = ?", [as_of]
            ).fetchone()[0]
        )
        daily_limit_rows = int(
            store.con.execute(
                "SELECT COUNT(*) FROM daily_limit WHERE trade_date = ?", [as_of]
            ).fetchone()[0]
        )
        daily_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT ts_code FROM daily WHERE trade_date = ?", [as_of]
            ).fetchall()
        }
        daily_basic_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT ts_code FROM daily_basic WHERE trade_date = ?", [as_of]
            ).fetchall()
        }
        daily_limit_codes = {
            str(row[0])
            for row in store.con.execute(
                "SELECT ts_code FROM daily_limit WHERE trade_date = ?", [as_of]
            ).fetchall()
        }
        stock_basic_matched = int(
            store.con.execute(
                f"SELECT COUNT(*) FROM {relation} c JOIN stock_basic b USING (ts_code)"
            ).fetchone()[0]
        )
        daily_basic_matched = int(
            store.con.execute(
                f"""
                SELECT COUNT(*) FROM {relation} c
                JOIN daily_basic d ON d.ts_code = c.ts_code AND d.trade_date = ?
                """,
                [as_of],
            ).fetchone()[0]
        )
        daily_limit_matched = int(
            store.con.execute(
                f"""
                SELECT COUNT(*) FROM {relation} c
                JOIN daily_limit d ON d.ts_code = c.ts_code AND d.trade_date = ?
                """,
                [as_of],
            ).fetchone()[0]
        )
    finally:
        store.con.unregister(relation)

    expected_moneyflow: set[tuple[str, str]] = set()
    actual_moneyflow: set[tuple[str, str]] = set()
    for code in candidate_codes["ts_code"].astype(str):
        history = store.history(code, as_of, 5)
        expected_moneyflow.update(
            (code, date) for date in history["trade_date"].astype(str)
        )
        moneyflow = store.moneyflow_tail(code, as_of, 5)
        if moneyflow is not None and not moneyflow.empty:
            actual_moneyflow.update(
                (code, date) for date in moneyflow["trade_date"].astype(str)
            )
    matched_moneyflow = len(expected_moneyflow & actual_moneyflow)

    source_daily_rows = (
        int(ingested_rows["daily"])
        if online and "daily" in ingested_rows
        else daily_rows
    )
    source_daily_basic_rows = (
        int(ingested_rows["daily_basic"])
        if online and "daily_basic" in ingested_rows
        else daily_basic_rows
    )
    source_daily_limit_rows = (
        int(ingested_rows["daily_limit"])
        if online and "daily_limit" in ingested_rows
        else daily_limit_rows
    )
    # stock_basic 只有当前状态，不能拿它反推历史日期的完整股票集合：未来新股会
    # 被误报为缺行情，已经退市但当日仍交易的股票又会被误报为多余。daily 是
    # Tushare 当日权威集合；daily_basic 必须与它完全一致，stk_limit 必须覆盖它。
    expected_market_codes = daily_codes
    market_expected_rows = len(expected_market_codes)

    def market_audit(
        codes: set[str], *, allow_extra: bool = False, invalid_codes: set[str] | None = None
    ) -> Dict[str, Any]:
        expected = sorted(expected_market_codes)
        missing_codes = sorted(expected_market_codes - codes)
        unexpected_codes = sorted(codes - expected_market_codes)
        matched_codes = expected_market_codes & codes
        invalid = sorted(invalid_codes or set())
        if not expected_market_codes:
            coverage = 1.0 if not missing_codes and not invalid else 0.0
        else:
            penalty = len(missing_codes) + len(invalid)
            if not allow_extra:
                penalty += len(unexpected_codes)
            coverage = max(0.0, (market_expected_rows - penalty) / market_expected_rows)
        return {
            "market_expected_rows": market_expected_rows,
            "market_expected_sample": expected[:20],
            "market_matched_rows": len(matched_codes),
            "market_missing_count": len(missing_codes),
            "market_missing_sample": missing_codes[:20],
            "market_unexpected_count": len(unexpected_codes),
            "market_unexpected_sample": unexpected_codes[:20],
            "market_invalid_count": len(invalid),
            "market_invalid_sample": invalid[:20],
            "market_coverage": coverage,
        }

    valid_historical_codes = lifecycle_codes | previously_traded_codes
    daily_market = market_audit(
        daily_codes,
        invalid_codes=daily_codes - valid_historical_codes,
    )
    daily_basic_market = market_audit(daily_basic_codes)
    # stk_limit 同一端点包含 ETF 等非股票代码，只检查股票日线是否全覆盖。
    daily_limit_market = market_audit(daily_limit_codes, allow_extra=True)
    missing_values = _candidate_missing_values(candidates)
    expected_values = candidate_count * len(_REQUIRED_CANDIDATE_FIELDS)
    cutoff = f"{as_of}T23:59:59+00:00"
    table_names = set(_REQUIRED_SCAN_TABLES)
    missing_tables = [name for name in _REQUIRED_SCAN_TABLES if name not in table_names]
    missing_dates: Dict[str, list[str]] = {}
    for table_name in ("daily", "daily_basic", "daily_limit"):
        count = int(
            store.con.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE trade_date = ?", [as_of]
            ).fetchone()[0]
        )
        if count == 0:
            missing_dates[table_name] = [as_of]
    max_date_row = store.con.execute("SELECT MAX(trade_date) FROM daily").fetchone()
    max_date = str(max_date_row[0]) if max_date_row and max_date_row[0] else None
    coverage_stock_count = int(daily_market["market_matched_rows"])
    coverage_rate = _coverage(coverage_stock_count, market_expected_rows)
    daily_limit_coverage = daily_limit_market["market_coverage"]
    return {
        "source": source,
        "as_of": as_of,
        "target_date": as_of,
        "max_date": max_date,
        "missing_tables": missing_tables,
        "missing_dates": missing_dates,
        "coverage_stock_count": coverage_stock_count,
        "coverage_rate": coverage_rate,
        "daily_limit_coverage": daily_limit_coverage,
        "candidate_pool_count": int(history_window.get("evaluated_count", candidate_count)),
        "history_window": dict(history_window),
        "context_filter": dict(context_filter),
        "data_cutoff_at": cutoff,
        "minimum_daily_rows": int(minimum_daily_rows),
        "daily": {
            "source": source,
            "data_date": as_of,
            "rows": daily_rows,
            "source_rows": source_daily_rows,
            "confirmed_rows": (
                int(expected_daily_rows)
                if expected_daily_rows is not None
                else None
            ),
            "expected_rows": market_expected_rows,
            "coverage": daily_market["market_coverage"],
            **daily_market,
        },
        "stock_basic": {
            "source": source,
            "data_date": as_of,
            "rows": stock_basic_rows,
            "matched_rows": stock_basic_matched,
            "expected_rows": candidate_count,
            "coverage": _coverage(stock_basic_matched, candidate_count),
        },
        "suspend_daily": {
            "source": source,
            "data_date": as_of,
            "rows": len(suspended_codes),
            "codes_sample": sorted(suspended_codes)[:20],
        },
        "daily_basic": {
            "source": source,
            "data_date": as_of,
            "rows": daily_basic_rows,
            "source_rows": source_daily_basic_rows,
            "matched_rows": daily_basic_matched,
            "expected_rows": candidate_count,
            "coverage": _coverage(daily_basic_matched, candidate_count),
            **daily_basic_market,
        },
        "daily_limit": {
            "source": source,
            "data_date": as_of,
            "rows": daily_limit_rows,
            "source_rows": source_daily_limit_rows,
            "matched_rows": daily_limit_matched,
            "expected_rows": candidate_count,
            "coverage": _coverage(daily_limit_matched, candidate_count),
            **daily_limit_market,
        },
        "moneyflow": {
            "source": source,
            "data_date": as_of,
            "rows": len(actual_moneyflow),
            "matched_rows": matched_moneyflow,
            "expected_rows": len(expected_moneyflow),
            "coverage": _coverage(matched_moneyflow, len(expected_moneyflow)),
        },
        "key_fields": {
            "fields": list(_REQUIRED_CANDIDATE_FIELDS),
            "rows": candidate_count,
            "missing_values": missing_values,
            "expected_values": expected_values,
            "missing_rate": _coverage(missing_values, expected_values),
        },
        "optional_fields": _optional_candidate_field_quality(candidates),
        "contexts": {
            "rows": len(contexts),
            "expected_rows": candidate_count,
            "coverage": _coverage(len(contexts), candidate_count),
        },
        "ingested_rows": dict(ingested_rows),
    }

_REQUIRED_SCAN_TABLES = (
    "trade_cal", "stock_basic", "daily", "daily_basic", "moneyflow", "daily_limit",
)


def _assert_required_scan_tables(store: Store) -> None:
    existing = {
        str(row[0])
        for row in store.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    missing = [name for name in _REQUIRED_SCAN_TABLES if name not in existing]
    if missing:
        raise RuntimeError(f"扫描数据缺少关键表: {', '.join(missing)}")



def prepare_scan_data(
    *,
    strategy_name: Optional[str] = None,
    online: bool = True,
    db_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    settings_override: Optional[Dict[str, Any]] = None,
    client: Optional[TushareClient] = None,
    as_of: Optional[str] = None,
    expected_daily_rows: Optional[int] = None,
) -> ScanPreparation:
    """准备并冻结评分输入，不执行规则评分或写扫描结果。

    overrides 允许运行时覆盖策略字段(供旧脚本 CLI 兼容层使用),支持:
    - price_max / min_amount_yi -> universe
    - top_n / candidate_limit
    """
    settings = settings_override or load_settings()
    strat_name = strategy_name or settings["engine"]["default_strategy"]
    strat = load_strategy(strat_name)

    ov = dict(overrides or {})
    uni = dict(strat.get("universe", {}) or {})
    if ov.get("price_max") is not None:
        uni["price_max"] = float(ov["price_max"])
    if ov.get("min_amount_yi") is not None:
        uni["min_amount_yi"] = float(ov["min_amount_yi"])
    strat["universe"] = uni
    if ov.get("top_n") is not None:
        strat["top_n"] = int(ov["top_n"])

    dbp = db_path or str(resolve_path(settings["data"]["db_path"]))
    bars = int(settings["engine"].get("history_bars", 150))
    cand_limit = int(ov.get("candidate_limit") or settings["engine"].get("candidate_limit", 260))
    price_max = uni.get("price_max")

    market_client = client or (_make_client(settings) if online else None)
    ingested_rows: Dict[str, int] = {}

    with Store(dbp, ensure_schema=False) as store:
        _assert_required_scan_tables(store)
        if as_of is None:
            actual_as_of, confirmed_rows, ingested_rows = _ensure_data(
                store, settings, online=online, client=market_client
            )
            if expected_daily_rows is None:
                expected_daily_rows = confirmed_rows
        else:
            actual_as_of = as_of
            if online:
                if market_client is None:
                    raise RuntimeError("在线准备市场数据时缺少 Tushare 客户端")
                ingested_rows = ingest_snapshot(store, market_client, actual_as_of)

        snap = store.snapshot(actual_as_of)
        if snap.empty:
            raise RuntimeError(f"as_of={actual_as_of} 本地无截面数据。")

        m = apply_universe(snap, strat.get("universe", {}) or {})
        ind = industry_heat(m)
        heat_map, rank_map, top_inds = industry_meta(ind)
        candidate_pool = build_candidates(m, ind, top_inds, cand_limit)

        if online and market_client is not None:
            ingested_rows["history"] = _backfill_history(
                store, market_client, candidate_pool, actual_as_of, bars
            )
        cand, history_window = _partition_candidates_by_history(
            store, candidate_pool, actual_as_of, bars
        )
        if online and market_client is not None:
            ingested_rows["moneyflow"] = _backfill_moneyflow(
                store, market_client, cand["ts_code"].tolist(), actual_as_of
            )

        contexts = _build_contexts(
            store,
            cand,
            heat_map,
            rank_map,
            top_inds,
            actual_as_of,
            bars,
            price_max,
        )
        context_codes = {context.ts_code for context in contexts}
        context_excluded_codes = (
            cand.loc[~cand["ts_code"].astype(str).isin(context_codes), "ts_code"]
            .astype(str)
            .tolist()
        )
        context_filter = {
            "evaluated_count": int(len(cand)),
            "eligible_count": len(context_codes),
            "excluded_count": len(context_excluded_codes),
            "excluded_codes": context_excluded_codes,
        }
        cand = cand.loc[cand["ts_code"].astype(str).isin(context_codes)].copy()
        cand.reset_index(drop=True, inplace=True)
        data_quality = _build_data_quality(
            store,
            as_of=actual_as_of,
            online=online,
            candidates=cand,
            contexts=contexts,
            minimum_daily_rows=int(settings["data"]["min_daily_rows"]),
            expected_daily_rows=expected_daily_rows,
            ingested_rows=ingested_rows,
            history_window=history_window,
            context_filter=context_filter,
        )

    return ScanPreparation(
        db_path=dbp,
        as_of=actual_as_of,
        strategy_name=strat_name,
        strategy=strat,
        online=online,
        candidates=cand.reset_index(drop=True),
        contexts=contexts,
        top_industries=ind.head(15).to_dict(orient="records"),
        snapshot_count=int(len(snap)),
        data_cutoff_at=data_quality["data_cutoff_at"],
        data_quality=data_quality,
        minimum_daily_rows=int(settings["data"]["min_daily_rows"]),
    )


def validate_scan_integrity(
    prepared: ScanPreparation, *, require_complete_sources: bool = False
) -> Dict[str, Any]:
    """在任何规则评分前检查冻结输入的基本完整性。"""
    if not prepared.as_of:
        raise RuntimeError("扫描数据没有明确的 as_of")
    if prepared.snapshot_count <= 0:
        raise RuntimeError(f"as_of={prepared.as_of} 的市场截面为空")
    if prepared.candidates.empty:
        raise RuntimeError(f"as_of={prepared.as_of} 的候选池为空")
    if not prepared.contexts:
        raise RuntimeError(f"as_of={prepared.as_of} 没有可评分的完整股票上下文")
    context_codes = {context.ts_code for context in prepared.contexts}
    candidate_codes = set(prepared.candidates["ts_code"].astype(str))
    if not context_codes.issubset(candidate_codes):
        raise RuntimeError("评分上下文混入冻结候选池之外的股票")
    payload = {
        "as_of": prepared.as_of,
        "snapshot_count": prepared.snapshot_count,
        "candidate_count": int(len(prepared.candidates)),
        "context_count": int(len(prepared.contexts)),
    }
    if not require_complete_sources:
        return payload
    audit = prepared.data_quality
    if not audit or audit.get("as_of") != prepared.as_of:
        raise RuntimeError("市场数据缺少与 as_of 一致的完整性审计")
    if not prepared.data_cutoff_at or audit.get("data_cutoff_at") != prepared.data_cutoff_at:
        raise RuntimeError("市场数据缺少一致的数据截止时间")
    warnings: list[str] = []
    daily = audit.get("daily") or {}
    if int(daily.get("source_rows") or 0) <= prepared.minimum_daily_rows:
        warnings.append(f"daily 行数未超过完整收盘阈值 {prepared.minimum_daily_rows}")
    confirmed_rows = daily.get("confirmed_rows")
    if (
        confirmed_rows is not None
        and int(daily.get("source_rows") or 0) < int(confirmed_rows)
    ):
        warnings.append(
            "daily 实际入库行数少于交易日确认行数 "
            f"{daily.get('source_rows')}/{confirmed_rows}"
        )
    for source_name in ("daily", "daily_basic", "daily_limit"):
        market_coverage = (audit.get(source_name) or {}).get("market_coverage")
        if market_coverage is None or not math.isclose(float(market_coverage), 1.0):
            warnings.append(f"{source_name} 全市场覆盖率未达到 100%")
    for source_name in ("daily", "stock_basic", "daily_basic", "daily_limit", "moneyflow"):
        metric = audit.get(source_name) or {}
        if metric.get("data_date") != prepared.as_of:
            warnings.append(f"{source_name} 数据日期与 as_of 不一致")
        coverage = metric.get("coverage")
        if coverage is None or not math.isclose(float(coverage), 1.0):
            warnings.append(f"{source_name} 候选覆盖率未达到 100%")
    missing_rate = (audit.get("key_fields") or {}).get("missing_rate")
    if missing_rate is None or not math.isclose(float(missing_rate), 0.0):
        warnings.append("候选池关键字段缺失率不为 0")
    payload["data_cutoff_at"] = prepared.data_cutoff_at
    payload["data_quality"] = audit
    payload["warnings"] = warnings
    return payload


def score_prepared_scan(
    prepared: ScanPreparation,
    *,
    run_id: Optional[str] = None,
    record: bool = True,
) -> ScanResult:
    """只对已准备的数据评分，并按需原子写入扫描与选股台账。"""
    scored = score_pool(prepared.contexts, prepared.strategy)
    passed = [stock for stock in scored if stock.passed]
    final = dedup_and_top(
        scored,
        max_per_industry=int((prepared.strategy.get("dedup", {}) or {}).get("max_per_industry", 2)),
        top_n=int(prepared.strategy.get("top_n", 6)),
        require_pass=True,
    )
    config_hash = _config_hash(prepared.strategy)
    candidate_hash = _candidate_hash(prepared.candidates)
    batch_key = f"{prepared.as_of}:{prepared.strategy_name}:{config_hash}"
    deterministic_run_id = hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:32]
    result = ScanResult(
        run_id=run_id or deterministic_run_id,
        as_of=prepared.as_of,
        strategy=prepared.strategy_name,
        config_hash=config_hash,
        candidate_hash=candidate_hash,
        data_cutoff_at=prepared.data_cutoff_at,
        candidate_count=int(len(prepared.candidates)),
        scored_count=int(len(scored)),
        passed_count=int(len(passed)),
        final=final,
        scored=scored,
        top_industries=prepared.top_industries,
    )
    if record:
        with Store(prepared.db_path) as store:
            _record_scan(store, result)
            _record_picks(store, result)
            if prepared.online:
                from .postmortem import backfill_returns
                backfill_returns(store, visible_max=prepared.as_of)
    return result


def run_scan(
    *,
    strategy_name: Optional[str] = None,
    online: bool = True,
    db_path: Optional[str] = None,
    record: bool = True,
    overrides: Optional[Dict[str, Any]] = None,
    as_of: Optional[str] = None,
    on_progress: Optional[Callable[..., None]] = None,
) -> ScanResult:
    """按“数据准备 → 完整性检查 → 评分”执行一次扫描。"""
    def emit(stage: str, step: int, total: int, message: str) -> None:
        if on_progress is not None:
            on_progress(stage=stage, step=step, total=total, message=message)

    total = 3
    emit("prepare", 1, total, "开始准备扫描数据")
    prepared = prepare_scan_data(
        strategy_name=strategy_name,
        online=online,
        db_path=db_path,
        overrides=overrides,
        as_of=as_of,
    )
    emit("prepare", 1, total, f"候选池准备完成: {len(prepared.candidates)} 只")
    emit("integrity", 2, total, "开始校验扫描数据完整性")
    validate_scan_integrity(prepared)
    emit("integrity", 2, total, "扫描数据完整性校验通过")
    emit("score", 3, total, "开始计算因子与综合评分")
    result = score_prepared_scan(prepared, record=record)
    emit("score", 3, total, "综合评分完成")
    return result


def scan_completion_payload(result: ScanResult) -> Dict[str, Any]:
    """把评分结果转换为最终原子提交所需的三张表数据。"""
    run_date = datetime.now().strftime("%Y%m%d%H%M%S")
    selected_codes = {stock.ts_code for stock in result.final}
    rows = []
    for rank, stock in enumerate(result.scored, start=1):
        rows.append({
            "run_id": result.run_id, "ts_code": stock.ts_code, "name": stock.name,
            "industry": stock.industry, "rank": rank, "total": float(stock.total),
            "passed": bool(stock.passed), "selected": stock.ts_code in selected_codes,
            "gate_reasons_json": json.dumps(stock.gate_reasons, ensure_ascii=False),
            "cat_scores_json": json.dumps(stock.cat_scores, ensure_ascii=False, default=str),
            "money_class": stock.money_class, "one_line": stock.one_line,
            "contrib_json": json.dumps(stock.contrib, ensure_ascii=False, default=str),
            "feat_json": json.dumps(stock.feat, ensure_ascii=False, default=str),
        })
    run_row = {
        "run_id": result.run_id, "run_date": run_date, "as_of": result.as_of,
        "strategy": result.strategy, "config_hash": result.config_hash,
        "candidate_hash": result.candidate_hash, "data_cutoff_at": result.data_cutoff_at,
        "candidate_count": result.candidate_count, "scored_count": result.scored_count,
        "passed_count": result.passed_count, "final_count": len(result.final),
        "top_industries_json": json.dumps(result.top_industries, ensure_ascii=False, default=str),
    }
    pick_rows = []
    for rank, s in enumerate(result.final, start=1):
        pick_rows.append({
            "run_date": run_date[:8], "as_of": result.as_of, "strategy": result.strategy,
            "ts_code": s.ts_code, "name": s.name, "industry": s.industry, "rank": rank,
            "total": float(s.total), "money_class": s.money_class, "one_line": s.one_line,
            "contrib_json": json.dumps(s.contrib, ensure_ascii=False),
            "feat_json": json.dumps(s.feat, ensure_ascii=False, default=str),
            "ret1": None, "ret3": None, "ret5": None, "ret10": None,
        })
    return {"run_row": run_row, "rows": pd.DataFrame(rows), "picks": pd.DataFrame(pick_rows),
            "as_of": result.as_of, "strategy": result.strategy}


def _record_scan(store: Store, result: ScanResult) -> None:
    payload = scan_completion_payload(result)
    store.record_scan(payload["run_row"], payload["rows"])


def _record_picks(store: Store, result: ScanResult) -> None:
    payload = scan_completion_payload(result)
    store.replace_picks(
        payload["as_of"], payload["strategy"], payload["picks"]
    )


def to_legacy_output(result: ScanResult, *, price_max: Optional[float]) -> Dict[str, Any]:
    """把 ScanResult 映射为旧脚本 under70_strict_mainup_scan.py 的 JSON 结构。

    旧消费方(skill/cron)读取 final[*] 的字段并做叙述,故此处按旧字段名回填,
    数据取自引擎 feat/contrib,保证下游零改动。
    """
    def _row(rank: int, s: ScoredStock) -> Dict[str, Any]:
        f = s.feat
        return {
            "ts_code": s.ts_code,
            "name": s.name,
            "industry": s.industry,
            "close": f.get("last_close"),
            "pct_chg": f.get("pct_chg"),
            "amount": f.get("amount"),
            "ret20": f.get("ret20"),
            "ret60": f.get("ret60"),
            "pos60": f.get("pos60"),
            "new20": bool(f.get("new20", 0)),
            "new60": bool(f.get("new60", 0)),
            "macd_dif": f.get("macd_dif"),
            "macd_dea": f.get("macd_dea"),
            "macd_hist": f.get("macd_hist"),
            "macd_bull": int(f.get("macd_bull", 0)),
            "weekly_bull": int(f.get("weekly_bull", 0)),
            "ma_stack": int(f.get("ma_stack", 0)),
            "vol5_20": f.get("vol5_20"),
            "vol20_60": f.get("vol20_60"),
            "amt5_20": f.get("amt5_20"),
            "vol_score": int(f.get("vol_health", 0)),
            "net5": f.get("net5"),
            "big5": f.get("big5"),
            "big_pos_days": (
                int(f.get("big_pos_days", 0))
                if f.get("big_pos_days") == f.get("big_pos_days")
                else 0
            ),
            "money_class": s.money_class,
            "score": round(float(s.total), 4),
            "one_line": s.one_line,
            "rank": rank,
            "contrib": {k: round(v, 4) for k, v in s.contrib.items()},
        }

    final_rows = [_row(i + 1, s) for i, s in enumerate(result.final)]
    return {
        "latest_trade_date": result.as_of,
        "price_max": price_max,
        "hard_filters": [
            "close < price_max", "exclude ST/*ST", "exclude 300/301",
            "exclude 688/689", "exclude BSE-like 8/4/9 prefixes",
        ],
        "top_industries": result.top_industries,
        "candidate_count": result.candidate_count,
        "structured_count": result.scored_count,
        "passed_count": result.passed_count,
        "final": final_rows,
        "engine": "quant_workbench",
        "strategy": result.strategy,
    }


def _cli() -> None:
    import argparse

    from .schedule import normalize_trade_date
    from .visibility import (
        LookaheadBlocked,
        ensure_visible,
        require_visible_as_of,
        resolve_window,
    )

    ap = argparse.ArgumentParser(description="Quant workbench 扫描入口")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--offline", action="store_true", help="仅用本地数据,不联网")
    ap.add_argument("--no-record", action="store_true", help="不写 picks 台账")
    ap.add_argument("--output", default=None, help="额外导出 JSON 结果")
    ap.add_argument(
        "--trade-date",
        default=None,
        help="截面交易日 YYYYMMDD,默认取可见日(基准日往前退 N 个开市日)",
    )
    args = ap.parse_args()

    # 日期口径(可见闸门):命令行同样不许拿隐藏窗口里的行情选股。
    # 默认日期只能是可见日;显式日期必须 <= 可见日;窗口算不出来直接退出,
    # 绝不回退成"本地最新交易日"。
    settings = load_settings()
    db_path = str(resolve_path(settings["data"]["db_path"]))
    with Store(db_path, ensure_schema=False) as store:
        window = resolve_window(store, settings, exchange=EXCHANGE)
    try:
        if args.trade_date is None:
            as_of = require_visible_as_of(window)
        else:
            as_of = ensure_visible(normalize_trade_date(args.trade_date), window)
    except LookaheadBlocked as exc:
        raise SystemExit(f"拒绝执行扫描:{exc}") from exc

    res = run_scan(
        strategy_name=args.strategy,
        online=not args.offline,
        record=not args.no_record,
        as_of=as_of,
    )
    summary = {
        "as_of": res.as_of,
        "strategy": res.strategy,
        "candidate_count": res.candidate_count,
        "scored_count": res.scored_count,
        "passed_count": res.passed_count,
        "final": [
            {"rank": i + 1, "ts_code": s.ts_code, "name": s.name,
             "industry": s.industry, "total": round(s.total, 4),
             "money_class": s.money_class, "one_line": s.one_line}
            for i, s in enumerate(res.final)
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    _cli()
