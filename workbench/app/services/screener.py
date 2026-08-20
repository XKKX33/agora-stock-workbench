"""全市场筛选服务:最新日线 + 近5日均量 + RSI6,过滤/排序/分页。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository
from app.services.kline import rsi_series
from engine.db import Store


class ScreenerService:
    """全市场筛选:按最新行情与指标过滤,返回分页结果。"""

    _SORT_COLUMNS = {
        "close": "close",
        "pct_chg": "pct_chg",
        "vol_ratio": "vol_ratio",
        "turnover_rate": "turnover_rate",
        "rsi6": "rsi6",
        "total_mv": "total_mv",
        "circ_mv": "circ_mv",
    }

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

    def list(
        self,
        *,
        pct_min: float | None = None,
        pct_max: float | None = None,
        vol_ratio_min: float | None = None,
        industry: str | None = None,
        sort: str = "pct_chg",
        order: str = "desc",
        page: int = 1,
        per_page: int = 30,
        run_id: str | None = None,
        as_of: str | None = None,
        strategy: str | None = None,
    ) -> dict:
        """筛选行情或精确读取固定扫描批次;两条路径不互相回退。"""
        if run_id is not None:
            if not as_of or not strategy:
                raise WorkbenchError("invalid_params", "固定批次查询必须同时提供 run_id、as_of、strategy", status_code=400)
            run, frame = MarketRepository(self.db_path).scan_batch(run_id, as_of=as_of, strategy=strategy)
            return self._scan_batch_payload(run, frame, page=page, per_page=per_page)
        if as_of is not None or strategy is not None:
            raise WorkbenchError("invalid_params", "固定批次查询必须提供 run_id、as_of、strategy", status_code=400)
        """筛选全市场最新行情,返回 as_of/items/meta。"""
        if page < 1 or per_page < 1 or per_page > 200:
            raise WorkbenchError(
                "invalid_params",
                "page 需 >= 1,per_page 需在 1~200 之间",
                status_code=400,
            )
        if sort not in self._SORT_COLUMNS:
            raise WorkbenchError(
                "invalid_params", f"不支持的排序字段: {sort}", status_code=400
            )
        if order not in ("asc", "desc"):
            raise WorkbenchError(
                "invalid_params", f"不支持的排序方向: {order}", status_code=400
            )
        self._ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            as_of = store.latest_date()
            frame = store.con.execute(
                """
                SELECT b.ts_code, b.name, b.industry,
                       d.close, d.pct_chg, d.vol, d.amount,
                       v.avg_vol_5,
                       db.turnover_rate, db.volume_ratio, db.total_mv, db.circ_mv
                FROM stock_basic b
                JOIN (
                    SELECT ts_code, trade_date, close, pct_chg, vol, amount,
                           ROW_NUMBER() OVER (
                               PARTITION BY ts_code ORDER BY trade_date DESC
                           ) AS _rn
                    FROM daily
                ) d ON d.ts_code = b.ts_code AND d._rn = 1
                LEFT JOIN (
                    SELECT ts_code, AVG(vol) AS avg_vol_5
                    FROM (
                        SELECT ts_code, vol,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ts_code ORDER BY trade_date DESC
                               ) AS _rn2
                        FROM daily
                    )
                    WHERE _rn2 <= 5
                    GROUP BY ts_code
                ) v ON v.ts_code = b.ts_code
                LEFT JOIN daily_basic db
                       ON db.ts_code = b.ts_code AND db.trade_date = d.trade_date
                """
            ).df()
            rsi6_map = self._latest_rsi6(store)
        frame["vol_ratio"] = frame["vol"] / frame["avg_vol_5"]
        frame["rsi6"] = frame["ts_code"].map(rsi6_map)
        # 过滤:pct_min/pct_max 作用于 pct_chg,vol_ratio_min 作用于 vol_ratio
        if pct_min is not None:
            frame = frame[frame["pct_chg"] >= pct_min]
        if pct_max is not None:
            frame = frame[frame["pct_chg"] <= pct_max]
        if vol_ratio_min is not None:
            frame = frame[frame["vol_ratio"] >= vol_ratio_min]
        if industry:
            frame = frame[frame["industry"] == industry]
        # 排序与分页
        frame = frame.sort_values(
            self._SORT_COLUMNS[sort], ascending=order == "asc", na_position="last"
        )
        total = len(frame)
        start = (page - 1) * per_page
        page_frame = frame.iloc[start : start + per_page]
        items = [
            {
                "ts_code": self._clean(row["ts_code"]),
                "name": self._clean(row["name"]),
                "industry": self._clean(row["industry"]),
                "close": self._clean(row["close"]),
                "pct_chg": self._clean(row["pct_chg"]),
                "vol_ratio": self._clean(row["vol_ratio"]),
                "turnover_rate": self._clean(row["turnover_rate"]),
                "rsi6": self._clean(row["rsi6"]),
                "total_mv": self._clean(row["total_mv"]),
                "circ_mv": self._clean(row["circ_mv"]),
            }
            for _, row in page_frame.iterrows()
        ]

        return {
            "as_of": as_of,
            "items": items,
            "meta": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": math.ceil(total / per_page) if total else 0,
            },
        }

    def _scan_batch_payload(self, run: dict, frame: pd.DataFrame, *, page: int, per_page: int) -> dict:
        if page < 1 or per_page < 1 or per_page > 200:
            raise WorkbenchError("invalid_params", "page 需 >= 1,per_page 需在 1~200 之间", status_code=400)
        frame = frame.copy()
        total = len(frame)
        start = (page - 1) * per_page
        rows = frame.iloc[start:start + per_page]
        items = []
        for _, row in rows.iterrows():
            items.append({
                "ts_code": self._clean(row.get("ts_code")), "name": self._clean(row.get("name")),
                "industry": self._clean(row.get("industry")), "rank": self._clean(row.get("rank")),
                "total": self._clean(row.get("total")), "passed": bool(row.get("passed")),
                "selected": bool(row.get("selected")), "money_class": self._clean(row.get("money_class")),
                "one_line": self._clean(row.get("one_line")),
            })
        return {
            "run_id": run.get("run_id"), "as_of": run.get("as_of"), "strategy": run.get("strategy"),
            "config_hash": run.get("config_hash"), "candidate_hash": run.get("candidate_hash"),
            "data_cutoff_at": run.get("data_cutoff_at"), "items": items,
            "meta": {"page": page, "per_page": per_page, "total": total,
                     "pages": math.ceil(total / per_page) if total else 0},
        }
        return {
            "as_of": as_of,
            "items": items,
            "meta": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": math.ceil(total / per_page) if total else 0,
            },
        }

    @staticmethod
    def _latest_rsi6(store) -> dict[str, float | None]:
        """每只股票按自身日线序列算 RSI6,返回 {ts_code: 最新值}。"""
        daily = store.con.execute(
            "SELECT ts_code, close FROM daily ORDER BY ts_code, trade_date"
        ).df()
        if daily.empty:
            return {}
        daily["_rsi6"] = daily.groupby("ts_code", sort=False)["close"].transform(
            lambda series: rsi_series(series, 6)
        )
        latest = daily.groupby("ts_code", sort=False).tail(1)
        return {
            str(ts_code): value
            for ts_code, value in zip(latest["ts_code"], latest["_rsi6"])
        }
