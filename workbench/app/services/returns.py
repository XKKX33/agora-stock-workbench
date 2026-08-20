from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from engine.config import load_settings
from engine.db import Store
from engine.returns import calculate_experiment_returns, returns_summary
from engine.visibility import LookaheadBlocked, require_visible_as_of, resolve_window

from app.errors import WorkbenchError

# API return serialization is rounded to ten decimal places; stored calculations stay raw.
_RETURN_SERIALIZATION_PRECISION = 10


def _serialize_gross_return(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, _RETURN_SERIALIZATION_PRECISION)
    return value


def serialize_return_payload(value: Any) -> Any:
    """把收益负载里的 gross_return 统一四舍五入到十位小数再返回。"""
    if isinstance(value, list):
        return [serialize_return_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _serialize_gross_return(item)
            if key.endswith("gross_return")
            else serialize_return_payload(item)
            for key, item in value.items()
        }
    return value


class ReturnsService:
    """Application boundary for independent experiment return calculations."""

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

    def calculate(self, *, run_id: str | None = None, exchange: str = "SSE") -> dict[str, Any]:
        with self._store() as store:
            window = resolve_window(store, load_settings(), exchange=exchange)
            # 窗口算不出来就报错,绝不退化成"不限上限"——那等于悄悄关掉可见闸门。
            try:
                visible_max = require_visible_as_of(window)
            except LookaheadBlocked as exc:
                raise WorkbenchError(exc.code, str(exc), status_code=409) from exc
            try:
                result = calculate_experiment_returns(
                    store, run_id=run_id, exchange=exchange, visible_max=visible_max
                )
            except KeyError as exc:
                raise WorkbenchError("experiment_not_found", str(exc), status_code=404) from exc
            summary = returns_summary(store, run_id=run_id)
        return {
            "job_id": f"returns:{run_id or 'all'}:{exchange}",
            "kind": "returns_calculation",
            "status": "succeeded",
            "run_id": run_id,
            "exchange": exchange,
            "visible_as_of": window.visible_as_of,
            "delay_sessions": window.delay_sessions,
            "rows_written": result.rows_written,
            "summary": serialize_return_payload(summary),
        }

    def detail(
        self,
        *,
        run_id: str | None = None,
        group_name: str | None = None,
        ts_code: str | None = None,
        horizon: str | None = None,
        as_of: str | None = None,
        entry_status: str | None = None,
    ) -> dict[str, Any]:
        with self._store() as store:
            items = store.experiment_returns(
                run_id=run_id,
                group_name=group_name,
                ts_code=ts_code,
                horizon=horizon,
                as_of=as_of,
                entry_status=entry_status,
            )
        return {"items": serialize_return_payload(items), "total": len(items)}

    def summary(
        self,
        *,
        run_id: str | None = None,
        as_of: str | None = None,
        group_name: str | None = None,
        ts_code: str | None = None,
        entry_status: str | None = None,
    ) -> dict[str, Any]:
        with self._store() as store:
            return serialize_return_payload(
                returns_summary(
                    store,
                    run_id=run_id,
                    as_of=as_of,
                    group_name=group_name,
                    ts_code=ts_code,
                    entry_status=entry_status,
                )
            )
