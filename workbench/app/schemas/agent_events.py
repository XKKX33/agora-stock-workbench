from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    run_id: str
    seq: int
    event_id: str
    event_type: str
    ts_code: str | None = None
    stage: str | None = None
    role: str | None = None
    round_no: int | None = None
    content_json: str = "{}"
    citations_json: str = "[]"
    status: str | None = None
    created_at: str | None = None


class AgentEventsResponse(BaseModel):
    run_id: str
    items: list[AgentEvent] = Field(default_factory=list)
    next_seq: int
    has_more: bool
