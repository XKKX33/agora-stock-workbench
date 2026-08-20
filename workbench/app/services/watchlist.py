"""自选股服务:列表(带最新行情与筛选)/添加/删除。

数据来自真实库:watchlist 表 + stock_basic + daily 最新一根日线。
缺失字段返回 None,绝不编造。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository


class WatchlistService:
    _SORT_COLUMNS = {
        "sort_order": "sort_order",
        "name": "name",
        "industry": "industry",
        "close": "close",
        "pct_chg": "pct_chg",
    }

    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def list(
        self,
        *,
        search: str | None = None,
        industry: str | None = None,
        sort: str = "sort_order",
        order: str = "asc",
        page: int = 1,
        per_page: int = 50,
    ) -> dict:
        """自选股列表 + 最新行情,支持按代码/名称搜索与按行业筛选。"""
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
        frame = self.repository.watchlist_quotes()
        if not frame.empty:
            if search:
                needle = search.casefold()
                mask = (
                    frame["ts_code"].fillna("").str.casefold().str.contains(needle, regex=False)
                    | frame["name"].fillna("").str.casefold().str.contains(needle, regex=False)
                    | frame["symbol"].fillna("").str.casefold().str.contains(needle, regex=False)
                )
                frame = frame[mask]
            if industry:
                frame = frame[frame["industry"] == industry]
            frame = frame.sort_values(
                self._SORT_COLUMNS[sort], ascending=order == "asc", na_position="last"
            )
        total = len(frame)
        start = (page - 1) * per_page
        page_frame = frame.iloc[start : start + per_page]
        return {
            "items": [self._row(row) for _, row in page_frame.iterrows()],
            "meta": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": math.ceil(total / per_page) if total else 0,
            },
        }

    def add(self, ts_code: str, note: str | None = None) -> dict:
        """加入自选股;股票不存在由仓储层抛 404,重复添加幂等。"""
        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            raise WorkbenchError(
                "invalid_params", "ts_code 不能为空", status_code=400
            )
        added = self.repository.add_watchlist(ts_code, note)
        return {"ts_code": ts_code, "added": added}

    def remove(self, ts_code: str) -> dict:
        """删除自选股;原本不存在也算删除成功(幂等)。"""
        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            raise WorkbenchError(
                "invalid_params", "ts_code 不能为空", status_code=400
            )
        removed = self.repository.remove_watchlist(ts_code)
        return {"ts_code": ts_code, "removed": removed}

    @staticmethod
    def _row(row: pd.Series) -> dict:
        return {
            "ts_code": WatchlistService._clean(row.get("ts_code")),
            "symbol": WatchlistService._clean(row.get("symbol")),
            "name": WatchlistService._clean(row.get("name")),
            "industry": WatchlistService._clean(row.get("industry")),
            "note": WatchlistService._clean(row.get("note")),
            "last_date": WatchlistService._clean(row.get("last_date")),
            "close": WatchlistService._clean(row.get("close")),
            "pct_chg": WatchlistService._clean(row.get("pct_chg")),
        }

    @staticmethod
    def _clean(value):
        """numpy/NaN 值转成可 JSON 序列化的值,缺失一律 None。"""
        if value is None:
            return None
        if isinstance(value, np.generic):
            value = value.item()
        try:
            return None if pd.isna(value) else value
        except (TypeError, ValueError):
            return value
