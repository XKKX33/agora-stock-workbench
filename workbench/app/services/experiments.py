from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from engine.db import Store
from engine.db_experiments import classify_entry_status, entry_status_predicate

from app.errors import WorkbenchError
from app.services.returns import serialize_return_payload


GROUPS = ("rule", "ai", "hybrid", "benchmark")


class ExperimentService:
    """实验台账只读服务。所有连接均禁止执行建表语句。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _store(self) -> Iterator[Store]:
        try:
            with Store(self.db_path, ensure_schema=False) as store:
                yield store
        except FileNotFoundError as exc:
            raise WorkbenchError(
                "database_unavailable",
                f"DuckDB 数据库不存在: {self.db_path}",
                status_code=503,
            ) from exc

    @staticmethod
    def _rows(cursor) -> list[dict[str, Any]]:
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _filters(
        *,
        as_of: str | None,
        group_name: str | None,
        ts_code: str | None,
        entry_status: str | None,
    ) -> tuple[str, list[Any]]:
        conditions = ["r.status = 'succeeded'"]
        params: list[Any] = []
        for column, value in (
            ("r.as_of", as_of),
            ("d.group_name", group_name),
            ("d.ts_code", ts_code),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if entry_status is not None:
            # 成交状态只存在于 experiment_returns,所以用 EXISTS 回看收益明细,
            # 判断规则和 engine 层完全一致,页面不会出现第二套算法。
            conditions.append(
                "EXISTS (SELECT 1 FROM experiment_returns e"
                " WHERE e.run_id = d.run_id AND e.group_name = d.group_name"
                f" AND e.ts_code = d.ts_code AND {entry_status_predicate(entry_status, alias='e')})"
            )
        return " AND ".join(conditions), params

    def list(
        self,
        *,
        as_of: str | None,
        group_name: str | None,
        ts_code: str | None,
        entry_status: str | None,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        where, params = self._filters(
            as_of=as_of,
            group_name=group_name,
            ts_code=ts_code,
            entry_status=entry_status,
        )
        offset = (page - 1) * per_page

        with self._store() as store:
            total = store.con.execute(
                f"""
                SELECT COUNT(*)
                FROM experiment_decisions d
                JOIN experiment_runs r ON r.run_id = d.run_id
                WHERE {where}
                """,
                params,
            ).fetchone()[0]
            cursor = store.con.execute(
                f"""
                SELECT r.as_of, r.data_cutoff_at, d.*
                FROM experiment_decisions d
                JOIN experiment_runs r ON r.run_id = d.run_id
                WHERE {where}
                ORDER BY r.as_of DESC, d.group_name ASC,
                         d.rank ASC NULLS LAST, d.run_id ASC, d.ts_code ASC
                LIMIT ? OFFSET ?
                """,
                [*params, per_page, offset],
            )
            items = self._rows(cursor)
            self._attach_returns(store, items)
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "per_page": per_page,
        }

    def detail(self, run_id: str) -> dict[str, Any]:
        with self._store() as store:
            run = store.experiment_run(run_id)
            if run is None:
                raise WorkbenchError(
                    "experiment_not_found",
                    f"实验批次不存在: {run_id}",
                    status_code=404,
                )
            cursor = store.con.execute(
                """
                SELECT *
                FROM experiment_decisions
                WHERE run_id = ?
                ORDER BY group_name ASC, rank ASC NULLS LAST, ts_code ASC
                """,
                [run_id],
            )
            items = self._rows(cursor)
            self._attach_returns(store, items)
        return {"run": run, "items": items}

    @staticmethod
    def _attach_returns(store: Store, items: list[dict[str, Any]]) -> None:
        """把 experiment_returns 挂到决策行上,决策表本身不再存成交与收益。"""
        if not items:
            return
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for run_id in {item["run_id"] for item in items}:
            for row in store.experiment_returns(run_id=run_id):
                key = (row["run_id"], row["group_name"], row["ts_code"])
                buckets.setdefault(key, []).append(row)
        for item in items:
            rows = buckets.get(
                (item["run_id"], item["group_name"], item["ts_code"]), []
            )
            item["entry_status"] = classify_entry_status(rows)
            item["entry_date"] = next(
                (row["entry_date"] for row in rows if row["entry_date"] is not None),
                None,
            )
            item["entry_price"] = next(
                (row["entry_price"] for row in rows if row["entry_price"] is not None),
                None,
            )
            item["returns"] = serialize_return_payload(
                {
                    row["horizon"]: {
                        key: row[key]
                        for key in (
                            "gross_return",
                            "status",
                            "reason",
                            "sell_date",
                            "sell_session",
                            "sell_price",
                        )
                    }
                    for row in rows
                }
            )


