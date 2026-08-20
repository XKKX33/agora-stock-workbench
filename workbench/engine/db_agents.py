"""Persistent storage for public Agent conversation events."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any


_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|secret|token|password|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{8,}|xai-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _secret_values() -> tuple[str, ...]:
    values = {
        value.strip()
        for key, value in os.environ.items()
        if value and (key.upper().endswith("API_KEY") or "SECRET" in key.upper())
    }
    return tuple(sorted((value for value in values if len(value) >= 4), key=len, reverse=True))


def _redact(value: Any, *, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED
            if _SECRET_KEY_RE.search(str(key))
            else _redact(item, secret_values=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secret_values=secret_values) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            result = result.replace(secret, _REDACTED)
        return _SECRET_VALUE_RE.sub(_REDACTED, result)
    return value


def _strict_json(value: Any, *, field: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是严格 JSON") from exc


def _event_json(value: Any, *, field: str, secret_values: tuple[str, ...]) -> str:
    if isinstance(value, str) and field.endswith("_json"):
        try:
            value = json.loads(
                value,
                parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{field} 必须是严格 JSON") from exc
    return _strict_json(_redact(value, secret_values=secret_values), field=field)


class AgentEventMixin:
    """Agent event persistence; DuckDB is the source of truth for replay."""

    def append_agent_event(self, event: dict) -> dict:
        if not isinstance(event, Mapping):
            raise TypeError("event 必须是对象")
        required = {"run_id", "event_id", "event_type"}
        missing = required - set(event)
        if missing:
            raise ValueError(f"Agent event 缺少字段: {sorted(missing)}")
        run_id = str(event["run_id"])
        if not run_id:
            raise ValueError("run_id 不能为空")
        secret_values = _secret_values()
        content_value = event.get("content", event.get("content_json", {}))
        citations_value = event.get("citations", event.get("citations_json", []))
        content_json = _event_json(
            content_value,
            field="content_json" if "content_json" in event and "content" not in event else "content",
            secret_values=secret_values,
        )
        citations_json = _event_json(
            citations_value,
            field="citations_json" if "citations_json" in event and "citations" not in event else "citations",
            secret_values=secret_values,
        )
        values = {
            "run_id": run_id,
            "event_id": str(event["event_id"]),
            "event_type": str(event["event_type"]),
            "ts_code": event.get("ts_code"),
            "stage": event.get("stage"),
            "role": event.get("role"),
            "round_no": event.get("round_no"),
            "content_json": content_json,
            "citations_json": citations_json,
            "status": event.get("status"),
            "created_at": event.get("created_at"),
        }
        for attempt in range(3):
            owns_transaction = not self._has_active_transaction()
            started = False
            try:
                if owns_transaction:
                    self.con.execute("BEGIN TRANSACTION")
                    started = True
                seq = int(
                    self.con.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_events WHERE run_id = ?",
                        [run_id],
                    ).fetchone()[0]
                )
                self.con.execute(
                    """
                    INSERT INTO agent_events (
                        run_id, seq, event_id, event_type, ts_code, stage, role,
                        round_no, content_json, citations_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        values["run_id"], seq, values["event_id"], values["event_type"],
                        values["ts_code"], values["stage"], values["role"], values["round_no"],
                        values["content_json"], values["citations_json"], values["status"],
                        values["created_at"],
                    ],
                )
                if owns_transaction:
                    self.con.execute("COMMIT")
                return {**values, "seq": seq}
            except Exception:
                if owns_transaction and started:
                    try:
                        self.con.execute("ROLLBACK")
                    except Exception:
                        pass
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def agent_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        if after_seq < 0:
            raise ValueError("after_seq 不能为负数")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        rows = self.con.execute(
            """
            SELECT run_id, seq, event_id, event_type, ts_code, stage, role,
                   round_no, content_json, citations_json, status, created_at
            FROM agent_events
            WHERE run_id = ? AND seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            [str(run_id), after_seq, limit],
        ).fetchall()
        columns = [item[0] for item in self.con.description]
        return [dict(zip(columns, row)) for row in rows]

    def agent_event_last_seq(self, run_id: str) -> int:
        row = self.con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM agent_events WHERE run_id = ?",
            [str(run_id)],
        ).fetchone()
        return int(row[0]) if row else 0


__all__ = ["AgentEventMixin"]
