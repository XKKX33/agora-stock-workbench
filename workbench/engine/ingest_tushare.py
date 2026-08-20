"""Tushare 数据摄取层。

职责:
- 拉取 trade_cal / stock_basic / suspend_d / daily / daily_basic / moneyflow,写入 DuckDB。
- 确认最新已收盘交易日(当日行数 > min_daily_rows)。
- 增量回补:只补本地缺失的交易日,已入库不重复请求。

纪律:
- 摄取只负责"搬运 + 落盘",不做打分、不做过滤逻辑。
- 所有网络请求带重试;失败返回空 DataFrame,由上层决定跳过。
- token 从环境变量读,绝不硬编码、绝不打印。
"""

from __future__ import annotations

import math
import logging
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from .db import Store


logger = logging.getLogger(__name__)

# Tushare 字段清单(集中管理,便于核对)
_F_DAILY = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
_F_BASIC = "ts_code,symbol,name,area,industry,market,list_date"
_F_LIFECYCLE = "ts_code,list_date,delist_date,list_status"
_F_DBASIC = "ts_code,trade_date,turnover_rate,volume_ratio,total_mv,circ_mv"
_F_MF = ("ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount,"
         "buy_elg_amount,sell_elg_amount")
_F_LIMIT = "ts_code,trade_date,up_limit,down_limit"
_F_SUSPEND = "ts_code,trade_date"
_LIMIT_COLUMNS = ("ts_code", "trade_date", "up_limit", "down_limit")
_SUSPEND_COLUMNS = ("ts_code", "trade_date")


def _field_names(fields: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in fields.split(",") if value.strip())


def _validated_snapshot_frame(
    frame: pd.DataFrame,
    endpoint: str,
    required_columns: tuple[str, ...],
    *,
    trade_date: str | None = None,
    numeric_columns: tuple[str, ...] = (),
    nullable_numeric_columns: tuple[str, ...] = (),
    nonnegative_numeric_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    """验证截面端点响应；全部端点验证完成后才允许写库。"""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise RuntimeError(f"{endpoint} 返回空数据")
    missing = set(required_columns) - set(frame.columns)
    if missing:
        raise RuntimeError(f"{endpoint} 缺少字段: {sorted(missing)}")
    if frame["ts_code"].isna().any() or frame["ts_code"].map(str).str.strip().eq("").any():
        raise RuntimeError(f"{endpoint}.ts_code 不可为空")
    keys = ["ts_code"]
    if trade_date is not None:
        if frame["trade_date"].isna().any():
            raise RuntimeError(f"{endpoint}.trade_date 不可为空")
        returned_dates = frame["trade_date"].map(str)
        if not returned_dates.eq(trade_date).all():
            raise RuntimeError(f"{endpoint} {trade_date} 返回了其他交易日数据")
        keys.append("trade_date")
    if frame.duplicated(keys).any():
        raise RuntimeError(f"{endpoint} 返回重复业务键")
    validated = frame.loc[:, required_columns].copy()
    for column in numeric_columns:
        raw = validated[column]
        numeric = pd.to_numeric(raw, errors="coerce")
        invalid = numeric.isna()
        if column in nullable_numeric_columns:
            # 保留上游真实缺失，但文本等无法解析的值仍视为非法。
            invalid &= ~raw.isna()
        if invalid.any() or not numeric.dropna().map(math.isfinite).all():
            raise RuntimeError(f"{endpoint}.{column} 包含非法数值")
        if column in nonnegative_numeric_columns and numeric.dropna().lt(0).any():
            raise RuntimeError(f"{endpoint}.{column} 包含非法数值")
        validated[column] = numeric
    return validated


class TushareClient:
    """薄封装:统一重试 + 限速。"""

    def __init__(self, token: Optional[str], retry: int = 3, sleep: float = 0.02,
                 timeout: int = 120):
        import tushare as ts

        self._ts = ts
        self.pro = ts.pro_api(token) if token else ts.pro_api()
        self.retry = max(1, int(retry))
        self.sleep = float(sleep)
        self.timeout = int(timeout)

    def _call(self, fn_name: str, **kw) -> pd.DataFrame:
        last_err: Optional[Exception] = None
        for attempt in range(self.retry):
            try:
                df = getattr(self.pro, fn_name)(**kw)
                if df is None:
                    last_err = RuntimeError(f"Tushare {fn_name} 返回空响应")
                    time.sleep(min(2.0, 0.3 * (attempt + 1)))
                    continue
                time.sleep(self.sleep)
                return df
            except Exception as e:  # noqa: BLE001 - 网络层需容错重试
                last_err = e
                time.sleep(min(2.0, 0.3 * (attempt + 1)))
        raise RuntimeError(
            f"Tushare {fn_name} 请求连续失败(重试 {self.retry} 次)"
        ) from None

    # ------------------------------------------------------------ 分项拉取
    def trade_cal(self, start: str, end: str, exchange: str = "SSE") -> pd.DataFrame:
        return self._call("trade_cal", exchange=exchange, start_date=start, end_date=end)

    def stock_basic(self) -> pd.DataFrame:
        return self._call("stock_basic", exchange="", list_status="L", fields=_F_BASIC)

    def stock_lifecycle(self) -> pd.DataFrame:
        frames = [
            self._call(
                "stock_basic",
                exchange="",
                list_status=status,
                fields=_F_LIFECYCLE,
            )
            for status in ("L", "D", "P")
        ]
        return pd.concat(frames, ignore_index=True)

    def daily(self, *, trade_date: str = "", ts_code: str = "",
              start_date: str = "", end_date: str = "") -> pd.DataFrame:
        kw = {"fields": _F_DAILY}
        if trade_date:
            kw["trade_date"] = trade_date
        if ts_code:
            kw["ts_code"] = ts_code
        if start_date:
            kw["start_date"] = start_date
        if end_date:
            kw["end_date"] = end_date
        return self._call("daily", **kw)

    def daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self._call("daily_basic", trade_date=trade_date, fields=_F_DBASIC)

    def stk_limit(self, trade_date: str) -> pd.DataFrame:
        return self._call("stk_limit", trade_date=trade_date, fields=_F_LIMIT)

    def suspend_d(self, as_of: str) -> pd.DataFrame:
        return self._call("suspend_d", trade_date=as_of, fields=_F_SUSPEND)

    def moneyflow(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._call("moneyflow", ts_code=ts_code, start_date=start_date,
                          end_date=end_date)


def confirm_latest_trade_date(client: TushareClient, min_rows: int) -> tuple[str, int]:
    """向 Tushare 确认最新已收盘交易日(行数 > min_rows 视为收盘确认)。"""
    today = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
    cal = client.trade_cal(start, today)
    if cal.empty:
        raise RuntimeError("trade_cal 拉取失败,无法确认交易日")
    open_days = sorted(
        cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist(), reverse=True
    )
    for d in open_days:
        sample = client.daily(trade_date=d)
        if not sample.empty and len(sample) > min_rows:
            return d, len(sample)
    raise RuntimeError("未找到已收盘确认的最新交易日")


def _validated_daily_limits(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    return _validated_snapshot_frame(
        frame,
        f"stk_limit {trade_date}",
        _LIMIT_COLUMNS,
        trade_date=trade_date,
        numeric_columns=("up_limit", "down_limit"),
    )


def _validated_suspend_daily(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """验证当日停牌清单；带完整字段的空表表示当日确实没有停牌。"""
    endpoint = f"suspend_d {trade_date}"
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeError(f"{endpoint} 返回类型不是 DataFrame")
    missing = set(_SUSPEND_COLUMNS) - set(frame.columns)
    if missing:
        raise RuntimeError(f"{endpoint} 缺少字段: {sorted(missing)}")

    validated = frame.loc[:, _SUSPEND_COLUMNS].copy()
    if validated.empty:
        return validated
    return _validated_snapshot_frame(
        validated,
        endpoint,
        ("ts_code", "trade_date"),
        trade_date=trade_date,
    )


def ingest_daily_limits(
    store: Store, client: TushareClient, trade_dates: Iterable[str]
) -> int:
    """逐日补采权威涨跌停价；任何一天无效都立即抛错。"""
    normalized: set[str] = set()
    for value in trade_dates:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("trade_dates 必须包含非空日期字符串")
        normalized.add(value.strip())

    total = 0
    for trade_date in sorted(normalized):
        frame = _validated_daily_limits(client.stk_limit(trade_date), trade_date)
        total += store.upsert(
            "daily_limit", frame, keys=("ts_code", "trade_date")
        )
    return total


def ingest_snapshot(store: Store, client: TushareClient, as_of: str) -> dict:
    """验证并原子替换某交易日的全市场截面。"""
    daily = _validated_snapshot_frame(
        client.daily(trade_date=as_of),
        "daily",
        _field_names(_F_DAILY),
        trade_date=as_of,
        numeric_columns=(
            "open", "high", "low", "close", "pre_close",
            "pct_chg", "vol", "amount",
        ),
    )
    basic = _validated_snapshot_frame(
        client.stock_basic(),
        "stock_basic",
        _field_names(_F_BASIC),
    )
    lifecycle = _validated_snapshot_frame(
        client.stock_lifecycle(),
        "stock_basic lifecycle",
        _field_names(_F_LIFECYCLE),
    )
    dbasic = _validated_snapshot_frame(
        client.daily_basic(trade_date=as_of),
        "daily_basic",
        _field_names(_F_DBASIC),
        trade_date=as_of,
        numeric_columns=("turnover_rate", "volume_ratio", "total_mv", "circ_mv"),
        nullable_numeric_columns=("volume_ratio",),
        nonnegative_numeric_columns=("volume_ratio",),
    )
    limits = _validated_daily_limits(client.stk_limit(as_of), as_of)
    suspended = _validated_suspend_daily(client.suspend_d(as_of), as_of)

    store.con.execute("BEGIN TRANSACTION")
    try:
        # stock_basic 表只表达当前上市集合，旧代码不能残留在完整性基准中。
        store.con.execute("DELETE FROM stock_basic")
        store.con.execute("DELETE FROM security_lifecycle")
        store.con.execute("DELETE FROM daily WHERE trade_date = ?", [as_of])
        store.con.execute("DELETE FROM daily_basic WHERE trade_date = ?", [as_of])
        store.con.execute("DELETE FROM daily_limit WHERE trade_date = ?", [as_of])
        store.con.execute("DELETE FROM suspend_daily WHERE trade_date = ?", [as_of])
        n_daily = store.upsert("daily", daily, keys=("ts_code", "trade_date"))
        n_basic = store.upsert("stock_basic", basic, keys=("ts_code",))
        n_lifecycle = store.upsert(
            "security_lifecycle", lifecycle, keys=("ts_code",)
        )
        n_dbasic = store.upsert(
            "daily_basic", dbasic, keys=("ts_code", "trade_date")
        )
        n_daily_limit = store.upsert(
            "daily_limit", limits, keys=("ts_code", "trade_date")
        )
        n_suspend_daily = store.upsert(
            "suspend_daily", suspended, keys=("ts_code", "trade_date")
        )
        store.con.execute("COMMIT")
    except Exception:
        store.con.execute("ROLLBACK")
        raise
    return {
        "daily": n_daily,
        "stock_basic": n_basic,
        "security_lifecycle": n_lifecycle,
        "daily_basic": n_dbasic,
        "daily_limit": n_daily_limit,
        "suspend_daily": n_suspend_daily,
    }


def ingest_history(store: Store, client: TushareClient, ts_codes: list[str],
                   start_date: str, end_date: str) -> int:
    """回补候选票的日线历史(单票逐个拉取)。返回写入总行数。"""
    total = 0
    for code in ts_codes:
        try:
            h = client.daily(ts_code=code, start_date=start_date, end_date=end_date)
            total += store.upsert("daily", h, keys=("ts_code", "trade_date"))
        except Exception as error:
            logger.warning("跳过历史行情摄取失败股票 %s: %s", code, type(error).__name__)
    return total


def ingest_calendar(store: Store, client: TushareClient, start: str, end: str,
                    exchange: str = "SSE") -> int:
    cal = client.trade_cal(start, end, exchange=exchange)
    if cal.empty:
        return 0
    cal = cal.rename(columns={"exchange": "exchange"})
    keep = cal[["exchange", "cal_date", "is_open"]].copy()
    return store.upsert("trade_cal", keep, keys=("exchange", "cal_date"))


def ingest_moneyflow(store: Store, client: TushareClient, ts_codes: list[str],
                     start_date: str, end_date: str) -> int:
    """摄取候选票资金流(事后确认字段)。返回写入总行数。"""
    total = 0
    for code in ts_codes:
        try:
            mf = client.moneyflow(code, start_date, end_date)
            total += store.upsert("moneyflow", mf, keys=("ts_code", "trade_date"))
        except Exception as error:
            logger.warning("跳过资金流摄取失败股票 %s: %s", code, type(error).__name__)
    return total
