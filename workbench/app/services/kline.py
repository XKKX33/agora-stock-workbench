"""K线行情服务:个股搜索、最新行情快照、历史K线与技术指标。

数据一律从 engine.db.Store 读取;缺失字段返回 None,绝不编造。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository
from engine.db import Store


def rsi_series(close: pd.Series, n: int) -> pd.Series:
    """RSI 序列:n 日 ewm(alpha=1/n, adjust=False) 平均涨跌幅。

    预热期(前 n 根)返回 NaN。ewm 从第一个样本就吐数,但用 1 天涨跌算 6 日 RSI
    只会得到 100 或 0——那是预热噪音,不是超买超卖信号,不能当指标发出去。
    """
    diff = close.diff()
    up = diff.clip(lower=0)
    down = -diff.clip(upper=0)
    avg_up = up.ewm(alpha=1 / n, adjust=False).mean()
    avg_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_up / avg_down
    series = 100 - 100 / (1 + rs)
    return _mask_warmup(series, n)


def _mask_warmup(series: pd.Series, periods: int) -> pd.Series:
    """让 ewm 指标和 `rolling(periods)` 口径一致:第 `periods` 根起才有值。

    rolling 天然遵守这个语义,ewm 没有——它从第一根就吐数。不统一的话同一份
    K 线里 ma5 第 5 根就有值、KDJ 却要等到第 10 根,读图的人没法判断哪个是真的。
    """
    if periods <= 1:
        return series
    out = series.copy()
    out.iloc[: min(periods - 1, len(out))] = np.nan
    return out


class KlineService:
    """行情K线服务:搜索 / 快照 / K线与技术指标。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _ensure_database(self) -> None:
        """数据库文件不存在时按约定抛 503。"""
        MarketRepository(self.db_path).ensure_database()

    @staticmethod
    def _clean(value):
        """numpy/NaN 值转成可 JSON 序列化的值,缺失一律 None。"""
        if value is None:
            return None
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        try:
            return None if pd.isna(value) else value
        except (TypeError, ValueError):
            return value

    def search(self, q: str | None = None, limit: int = 20) -> dict:
        """按 symbol/name/ts_code 模糊搜索;q 为空返回按成交额前 limit 只。"""
        self._ensure_database()
        query = (q or "").strip()
        pattern = f"%{query}%"
        with Store(self.db_path, ensure_schema=False) as store:
            frame = store.con.execute(
                """
                SELECT b.ts_code, b.symbol, b.name, b.industry,
                       d.trade_date AS last_date, d.close, d.pct_chg, d.amount
                FROM stock_basic b
                JOIN (
                    SELECT ts_code, trade_date, close, pct_chg, amount,
                           ROW_NUMBER() OVER (
                               PARTITION BY ts_code ORDER BY trade_date DESC
                           ) AS _rn
                    FROM daily
                ) d ON d.ts_code = b.ts_code AND d._rn = 1
                WHERE length(?) = 0
                   OR b.symbol ILIKE ?
                   OR b.name ILIKE ?
                   OR b.ts_code ILIKE ?
                """,
                [query, pattern, pattern, pattern],
            ).df()
        if query:
            # 优先返回 symbol 前缀匹配的股票,其余按成交额降序
            frame["_prefix"] = frame["symbol"].fillna("").str.startswith(query)
            frame = frame.sort_values(
                ["_prefix", "amount"], ascending=[False, False], na_position="last"
            ).drop(columns=["_prefix"])
        else:
            frame = frame.sort_values("amount", ascending=False, na_position="last")
        items = []
        for row in frame.head(limit).to_dict(orient="records"):
            items.append(
                {
                    "ts_code": self._clean(row["ts_code"]),
                    "symbol": self._clean(row["symbol"]),
                    "name": self._clean(row["name"]),
                    "industry": self._clean(row["industry"]),
                    "last_date": self._clean(row["last_date"]),
                    "close": self._clean(row["close"]),
                    "pct_chg": self._clean(row["pct_chg"]),
                }
            )
        return {"items": items}

    def quote(self, ts_code: str) -> dict:
        """基础信息 + 最新一根日线 + 近5日 + 当日 daily_basic + 资金流最近10条。"""
        self._ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            info = store.con.execute(
                """
                SELECT ts_code, symbol, name, area, industry, market, list_date
                FROM stock_basic
                WHERE ts_code = ?
                """,
                [ts_code],
            ).df()
            if info.empty:
                raise WorkbenchError(
                    "stock_not_found", f"未找到股票 {ts_code}", status_code=404
                )
            latest = store.con.execute(
                """
                SELECT trade_date, open, high, low, close, pre_close,
                       pct_chg, vol, amount
                FROM daily
                WHERE ts_code = ?
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                [ts_code],
            ).df()
            # 以该股票自身最新交易日为基准(可能早于全库最新日)
            as_of = str(latest.iloc[0]["trade_date"]) if not latest.empty else None
            recent5 = (
                store.history(ts_code, as_of, 5) if as_of is not None else pd.DataFrame()
            )
            basic = (
                store.con.execute(
                    """
                    SELECT turnover_rate, volume_ratio, total_mv, circ_mv
                    FROM daily_basic
                    WHERE ts_code = ? AND trade_date = ?
                    """,
                    [ts_code, as_of],
                ).df()
                if as_of is not None
                else pd.DataFrame()
            )
            moneyflow = (
                store.moneyflow_tail(ts_code, as_of, 10)
                if as_of is not None
                else pd.DataFrame()
            )
        base = info.iloc[0]
        quote_row = {
            "trade_date": None,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "pre_close": None,
            "pct_chg": None,
            "vol": None,
            "amount": None,
            "turnover_rate": None,
            "volume_ratio": None,
            "total_mv": None,
            "circ_mv": None,
        }
        if not latest.empty:
            quote_row.update(
                {key: self._clean(value) for key, value in latest.iloc[0].items()}
            )
        if not basic.empty:
            quote_row.update(
                {key: self._clean(value) for key, value in basic.iloc[0].items()}
            )
        return {
            "ts_code": self._clean(base["ts_code"]),
            "symbol": self._clean(base["symbol"]),
            "name": self._clean(base["name"]),
            "industry": self._clean(base["industry"]),
            "market": self._clean(base["market"]),
            "list_date": self._clean(base["list_date"]),
            "quote": quote_row,
            "recent5": self._records(recent5),
            "moneyflow": self._records(moneyflow),
        }

    def kline(self, ts_code: str, days: int = 250) -> dict:
        """升序历史K线 + 技术指标;指标算不出的位置返回 None。"""
        if days < 1:
            raise WorkbenchError(
                "invalid_params", f"days 需 >= 1,收到 {days}", status_code=400
            )
        base = self.quote(ts_code)
        with Store(self.db_path, ensure_schema=False) as store:
            frame = store.history(ts_code, store.latest_date(), days)
        frame = self._indicators(frame)
        return {
            "ts_code": base["ts_code"],
            "symbol": base["symbol"],
            "name": base["name"],
            "industry": base["industry"],
            "quote": base["quote"],
            "bars": self._bars(frame),
        }

    @staticmethod
    def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
        """给日线追加 MA/MACD/KDJ/RSI/BOLL 指标列。"""
        if frame.empty:
            return frame
        close = frame["close"]
        low = frame["low"]
        high = frame["high"]
        # 均线
        frame["ma5"] = close.rolling(5).mean()
        frame["ma10"] = close.rolling(10).mean()
        frame["ma20"] = close.rolling(20).mean()
        frame["ma60"] = close.rolling(60).mean()
        # MACD:ema12/ema26, dif=ema12-ema26, dea=dif 9 日 ema, macd=(dif-dea)*2
        # 预热掩码是必须的:ewm 从第一根就给数,首日 ema12==ema26==close 会让
        # dif/macd 恰好是 0.0——画在图上是贴着零轴的线,像"多空平衡"的真实信号,
        # 其实只是还没算出来。dif 第 26 根起成立;dea 要 9 个 dif,即第 34 根起。
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        frame["ema12"] = _mask_warmup(ema12, 12)
        frame["ema26"] = _mask_warmup(ema26, 26)
        frame["dif"] = _mask_warmup(dif, 26)
        dea = dif.ewm(span=9, adjust=False).mean()
        frame["dea"] = _mask_warmup(dea, 26 + 8)
        frame["macd"] = _mask_warmup((dif - dea) * 2, 26 + 8)
        # KDJ(9 日):RSV=(close-llv)/(hhv-llv)*100,K/D 用 ewm(alpha=1/3)
        # rsv 前 8 根是 NaN,但 ewm 会跳过 NaN 继续算,等于用不足 9 根的样本出 K 值。
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        frame["k"] = _mask_warmup(k, 9)
        frame["d"] = _mask_warmup(d, 9)
        frame["j"] = 3 * frame["k"] - 2 * frame["d"]
        # RSI6/12/24
        for n in (6, 12, 24):
            frame[f"rsi{n}"] = rsi_series(close, n)
        # BOLL:20 日均 ± 2 倍标准差(ddof=0)
        mid = close.rolling(20).mean()
        std = close.rolling(20).std(ddof=0)
        frame["boll_mid"] = mid
        frame["boll_upper"] = mid + 2 * std
        frame["boll_lower"] = mid - 2 * std
        return frame

    def _bars(self, frame: pd.DataFrame) -> list[dict]:
        keys = [
            "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
            "ma5", "ma10", "ma20", "ma60",
            "ema12", "ema26", "dif", "dea", "macd",
            "k", "d", "j",
            "rsi6", "rsi12", "rsi24",
            "boll_mid", "boll_upper", "boll_lower",
        ]
        return [
            {key: self._clean(row.get(key)) for key in keys}
            for _, row in frame.iterrows()
        ]

    def _records(self, frame: pd.DataFrame) -> list[dict]:
        """DataFrame 转记录列表,缺值置 None,去掉冗余的 ts_code 列。"""
        if frame is None or frame.empty:
            return []
        return [
            {key: self._clean(value) for key, value in row.items() if key != "ts_code"}
            for row in frame.to_dict(orient="records")
        ]
