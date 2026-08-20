from __future__ import annotations

import json
import threading

import pytest

from app.services.agents import AgentEventBus
from engine.db import Store


pytestmark = pytest.mark.api


def _run(run_id: str, status: str = "succeeded") -> dict:
    return {
        "run_id": run_id,
        "as_of": "20260809",
        "status": status,
        "stage": "done" if status == "succeeded" else "failed",
        "candidates": 1,
        "depth": 1,
        "final_count": 1,
        "progress_json": "{}",
        "created_at": "2026-08-09T00:00:00+00:00",
        "started_at": "2026-08-09T00:00:01+00:00",
        "finished_at": "2026-08-09T00:01:00+00:00",
        "heartbeat_at": "2026-08-09T00:01:00+00:00",
        "error_json": json.dumps({"type": "AgentOutputError", "message": "public failure"})
        if status == "failed"
        else None,
        "result_json": None,
    }


def _event(run_id: str, event_id: str, event_type: str = "message.completed") -> dict:
    return {
        "run_id": run_id,
        "event_id": event_id,
        "event_type": event_type,
        "stage": "debate",
        "role": "bull",
        "round_no": 1,
        "content": {"summary": "public"},
        "citations": [],
        "status": "completed",
        "created_at": "2026-08-09T00:00:02+00:00",
    }


def test_agent_events_endpoint_supports_after_seq(client, db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("replay-run"))
        store.append_agent_event(_event("replay-run", "event-1"))
        store.append_agent_event(_event("replay-run", "event-2", "run.completed"))

    response = client.get("/api/agents/jobs/replay-run/events", params={"after_seq": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "replay-run"
    assert [item["seq"] for item in payload["items"]] == [2]
    assert payload["next_seq"] == 2
    assert payload["has_more"] is False


def test_agent_events_endpoint_rejects_invalid_paging(client):
    response = client.get("/api/agents/jobs/missing/events", params={"after_seq": -1})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_after_seq"


def test_agent_stream_returns_sse_headers(client, db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("header-run"))
        store.append_agent_event(_event("header-run", "event-1", "run.completed"))

    response = client.get("/api/agents/jobs/header-run/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"


def test_agent_stream_replays_persisted_events(client, db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("stream-run"))
        store.append_agent_event(_event("stream-run", "event-1"))
        store.append_agent_event(_event("stream-run", "event-2", "run.completed"))

    response = client.get("/api/agents/jobs/stream-run/stream", params={"after_seq": 1})
    chunks = response.text.strip().split("\n\n")

    assert len(chunks) == 1
    assert "id: 2" in chunks[0]
    assert "event: run.completed" in chunks[0]
    data = next(line[6:] for line in chunks[0].splitlines() if line.startswith("data: "))
    assert json.loads(data)["event_id"] == "event-2"


def test_agent_stream_returns_structured_failure_event(client, db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("failed-run", "failed"))
        store.append_agent_event(_event("failed-run", "event-failed", "run.failed"))

    response = client.get("/api/agents/jobs/failed-run/stream")

    assert response.status_code == 200
    assert "event: run.failed" in response.text
    assert "public failure" not in response.text


def test_agent_events_and_stream_keep_special_routes_ahead_of_job_lookup(client, db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("route-run"))

    assert client.get("/api/agents/jobs/route-run/events").status_code == 200
    assert client.get("/api/agents/jobs/route-run/stream").status_code == 200

def test_agent_event_bus_close_is_scoped_to_subscription(db_path):
    with Store(db_path, ensure_schema=True) as store:
        store.record_agent_run(_run("multi-subscriber-run"))
        store.append_agent_event(_event("multi-subscriber-run", "event-1"))

    bus = AgentEventBus(db_path)
    second = bus.subscribe("multi-subscriber-run", after_seq=1)
    assert next(second) is None

    first_ready = threading.Event()
    close_first = threading.Event()
    first_closed = threading.Event()

    def first_stream() -> None:
        bus.subscribe("multi-subscriber-run", after_seq=1)
        first_ready.set()
        close_first.wait(timeout=5)
        bus.close("multi-subscriber-run")
        first_closed.set()

    worker = threading.Thread(target=first_stream)
    worker.start()
    assert first_ready.wait(timeout=5)
    close_first.set()
    assert first_closed.wait(timeout=5)

    published = bus.publish(_event("multi-subscriber-run", "event-2"))
    assert next(second) == published
    second.close()
    worker.join(timeout=5)
    assert not worker.is_alive()