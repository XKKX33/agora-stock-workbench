"""Tushare 数据摄取层。

职责:
- 拉取 trade_cal / stock_basic / daily / daily_basic / moneyflow,写入 DuckDB。
- 确认最新已收盘交易日(当日行数 > min_daily_rows)。
- 增量回补:只补本地缺失的交易日,已入库不重复请求。

纪律:
- 摄取只负责"搬运 + 落盘",不做打分、不做过滤逻辑。
- 所有网络请求带重试;失败返回空 DataFrame,由上层决定跳过。
- token 从环境变量读,绝不硬编码、绝不打印。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from .db import Store

# Tushare 字段清单(集中管理,便于核对)
_F_DAILY = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
_F_BASIC = "ts_code,symbol,name,area,industry,market,list_date"
_F_DBASIC = "ts_code,trade_date,turnover_rate,volume_ratio,total_mv,circ_mv"
_F_MF = ("ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount,"
         "buy_elg_amount,sell_elg_amount")
_F_LIMIT = "ts_code,trade_date,up_limit,down_limit"


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
                    return pd.DataFrame()
                time.sleep(self.sleep)
                return df
            except Exception as e:  # noqa: BLE001 - 网络层需容错重试
                last_err = e
                time.sleep(min(2.0, 0.3 * (attempt + 1)))
        # 三次失败:返回空,由上层决定跳过
        print(f"[ingest] {fn_name} 失败(重试{self.retry}次): {last_err}")
        return pd.DataFrame()

    # ------------------------------------------------------------ 分项拉取
    def trade_cal(self, start: str, end: str, exchange: str = "SSE") -> pd.DataFrame:
        return self._call("trade_cal", exchange=exchange, start_date=start, end_date=end)

    def stock_basic(self) -> pd.DataFrame:
        return self._call("stock_basic", exchange="", list_status="L", fields=_F_BASIC)

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


def ingest_snapshot(store: Store, client: TushareClient, as_of: str) -> dict:
    """摄取某交易日的全市场截面(daily + basic + daily_basic)。"""
    daily = client.daily(trade_date=as_of)
    basic = client.stock_basic()
    dbasic = client.daily_basic(trade_date=as_of)
    daily_limit = client.stk_limit(trade_date=as_of)

    n_daily = store.upsert("daily", daily, keys=("ts_code", "trade_date"))
    n_basic = store.upsert("stock_basic", basic, keys=("ts_code",))
    n_dbasic = store.upsert("daily_basic", dbasic, keys=("ts_code", "trade_date"))
    n_daily_limit = store.upsert(
        "daily_limit", daily_limit, keys=("ts_code", "trade_date")
    )
    return {
        "daily": n_daily,
        "stock_basic": n_basic,
        "daily_basic": n_dbasic,
        "daily_limit": n_daily_limit,
    }


def ingest_history(store: Store, client: TushareClient, ts_codes: list[str],
                   start_date: str, end_date: str) -> int:
    """回补候选票的日线历史(单票逐个拉取)。返回写入总行数。"""
    total = 0
    for code in ts_codes:
        h = client.daily(ts_code=code, start_date=start_date, end_date=end_date)
        total += store.upsert("daily", h, keys=("ts_code", "trade_date"))
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
        mf = client.moneyflow(code, start_date, end_date)
        total += store.upsert("moneyflow", mf, keys=("ts_code", "trade_date"))
    return total
