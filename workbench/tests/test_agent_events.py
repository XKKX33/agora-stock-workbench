"""Agent public-event persistence tests using temporary DuckDB files."""

from __future__ import annotations

import json

import pytest

from engine.db import Store


def _event(run_id: str, event_id: str, *, content=None, citations=None) -> dict:
    return {
        "run_id": run_id,
        "event_id": event_id,
        "event_type": "message.completed",
        "ts_code": "000001.SZ",
        "stage": "analysis",
        "role": "trend",
        "round_no": 1,
        "content": {"summary": "public summary"} if content is None else content,
        "citations": [] if citations is None else citations,
        "status": "completed",
        "created_at": "2026-08-09T09:00:00+08:00",
    }


def test_agent_events_have_monotonic_sequence(tmp_path):
    with Store(tmp_path / "events.duckdb") as store:
        first = store.append_agent_event(_event("run-1", "event-1"))
        second = store.append_agent_event(_event("run-1", "event-2"))
        other = store.append_agent_event(_event("run-2", "event-3"))

        assert first["seq"] == 1
        assert second["seq"] == 2
        assert other["seq"] == 1
        assert store.agent_event_last_seq("run-1") == 2
        assert store.agent_event_last_seq("run-2") == 1


def test_agent_events_resume_after_sequence(tmp_path):
    with Store(tmp_path / "resume.duckdb") as store:
        for number in range(1, 5):
            store.append_agent_event(_event("run-1", f"event-{number}"))

        resumed = store.agent_events("run-1", after_seq=2, limit=2)
        assert [item["seq"] for item in resumed] == [3, 4]
        assert [item["event_id"] for item in resumed] == ["event-3", "event-4"]
        assert json.loads(resumed[0]["content_json"]) == {"summary": "public summary"}


def test_agent_event_payload_does_not_contain_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_AI_API_KEY", "do-not-persist-this-key")
    with Store(tmp_path / "redaction.duckdb") as store:
        saved = store.append_agent_event(
            _event(
                "run-1",
                "event-1",
                content={
                    "summary": "safe",
                    "api_key": "do-not-persist-this-key",
                    "nested": {"authorization": "Bearer do-not-persist-this-key"},
                },
                citations=[{"url": "https://example.test", "token": "do-not-persist-this-key"}],
            )
        )
        persisted = json.dumps(saved)

        assert "do-not-persist-this-key" not in persisted
        assert json.loads(saved["content_json"]) == {
            "summary": "safe",
            "api_key": "[REDACTED]",
            "nested": {"authorization": "[REDACTED]"},
        }
        assert json.loads(saved["citations_json"]) == [
            {"url": "https://example.test", "token": "[REDACTED]"}
        ]


def test_agent_event_json_rejects_non_json_values(tmp_path):
    with Store(tmp_path / "strict-json.duckdb") as store:
        with pytest.raises(ValueError):
            store.append_agent_event(_event("run-1", "event-1", content={"bad": {1, 2}}))

        assert store.agent_event_last_seq("run-1") == 0


def test_schema_migration_adds_agent_events_without_deleting_agent_runs(tmp_path):
    db_path = tmp_path / "migration.duckdb"
    with Store(db_path) as store:
        store.record_agent_run(
            {
                "run_id": "legacy-run",
                "status": "succeeded",
                "stage": "done",
                "result_json": '{"final": []}',
            }
        )
        before = store.get_agent_run("legacy-run")
        assert before is not None

    with Store(db_path) as store:
        assert store.get_agent_run("legacy-run") == before
        columns = {
            row[1]
            for row in store.con.execute("PRAGMA table_info('agent_events')").fetchall()
        }
        assert columns == {
            "run_id",
            "seq",
            "event_id",
            "event_type",
            "ts_code",
            "stage",
            "role",
            "round_no",
            "content_json",
            "citations_json",
            "status",
            "created_at",
        }


def test_repeated_appends_keep_sequence_and_rows(tmp_path):
    with Store(tmp_path / "repeated.duckdb") as store:
        for number in range(1, 101):
            store.append_agent_event(_event("run-1", f"event-{number}"))

        rows = store.agent_events("run-1", limit=500)
        assert len(rows) == 100
        assert [item["seq"] for item in rows] == list(range(1, 101))
