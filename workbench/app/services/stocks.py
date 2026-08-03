from __future__ import annotations

import json
import math

import pandas as pd

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository


class StocksService:
    _JSON_FIELDS = {
        "gate_reasons_json": ("gate_reasons", []),
        "cat_scores_json": ("category_scores", {}),
        "contrib_json": ("factors", {}),
        "feat_json": ("features", {}),
    }

    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def list(
        self,
        *,
        page: int,
        per_page: int,
        passed: bool | None,
        selected: bool | None,
        industry: str | None,
        search: str | None,
        sort: str,
        order: str,
    ) -> dict:
        run, frame = self.repository.latest_scan_rows()
        data = frame.copy()
        if passed is not None:
            data = data[data["passed"] == passed]
        if selected is not None:
            data = data[data["selected"] == selected]
        if industry:
            data = data[data["industry"] == industry]
        if search:
            needle = search.casefold()
            mask = (
                data["ts_code"].fillna("").str.casefold().str.contains(needle, regex=False)
                | data["name"].fillna("").str.casefold().str.contains(needle, regex=False)
            )
            data = data[mask]
        ascending = order == "asc"
        data = data.sort_values(sort, ascending=ascending, na_position="last")
        total = len(data)
        start = (page - 1) * per_page
        page_frame = data.iloc[start : start + per_page]
        return {
            "run_id": run["run_id"],
            "as_of": run["as_of"],
            "items": [self._row(row) for _, row in page_frame.iterrows()],
            "meta": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": math.ceil(total / per_page) if total else 0,
            },
        }

    def detail(self, ts_code: str) -> dict:
        run, frame = self.repository.latest_scan_rows()
        match = frame[frame["ts_code"] == ts_code]
        if match.empty:
            raise WorkbenchError(
                "stock_not_found",
                f"最近一次扫描中不存在股票 {ts_code}",
                status_code=404,
            )
        payload = self._row(match.iloc[0])
        payload["as_of"] = run["as_of"]
        payload["history"] = self._records(
            self.repository.history(ts_code, str(run["as_of"]), 120)
        )
        payload["moneyflow"] = self._records(
            self.repository.moneyflow(ts_code, str(run["as_of"]), 10)
        )
        return payload

    def _row(self, row: pd.Series) -> dict:
        payload = {
            "ts_code": row["ts_code"],
            "name": row["name"],
            "industry": row["industry"],
            "rank": int(row["rank"]),
            "total": float(row["total"]),
            "passed": bool(row["passed"]),
            "selected": bool(row["selected"]),
            "money_class": row["money_class"],
            "one_line": row["one_line"],
        }
        for source, (target, default) in self._JSON_FIELDS.items():
            value = row[source]
            payload[target] = default if pd.isna(value) or value == "" else json.loads(value)
        return payload

    @staticmethod
    def _records(frame: pd.DataFrame) -> list[dict]:
        clean = frame.where(pd.notna(frame), None)
        return clean.to_dict(orient="records")
