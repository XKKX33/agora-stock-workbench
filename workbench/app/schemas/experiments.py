from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ExperimentGroup = Literal["rule", "ai", "hybrid", "benchmark"]
EntryStatus = Literal["pending_entry", "filled", "entry_unavailable"]


class ExperimentRun(BaseModel):
    run_id: str
    as_of: str | None = None
    data_cutoff_at: str | None = None
    status: str
    strategy_name: str
    strategy_version: str | None = None
    model: str | None = None
    temperature: float | None = None
    prompt_version: str | None = None
    candidate_hash: str | None = None
    candidate_count: int | None = None
    final_count: int | None = None
    hybrid_rule_weight: float | None = None
    hybrid_ai_weight: float | None = None
    created_at: str | None = None
    finished_at: str | None = None
    error_json: str | None = None


class HorizonReturn(BaseModel):
    """单个期限的收益结果;算不出就带 status/reason,不写 0。"""

    gross_return: float | None = None
    status: str | None = None
    reason: str | None = None
    sell_date: str | None = None
    sell_session: str | None = None
    sell_price: float | None = None


class ExperimentDecision(BaseModel):
    run_id: str
    group_name: ExperimentGroup
    ts_code: str
    name: str | None = None
    industry: str | None = None
    rank: int | None = None
    rule_score: float | None = None
    ai_score: float | None = None
    hybrid_score: float | None = None
    reason_json: str | None = None
    risk_json: str | None = None
    # 成交与各期收益都来自 experiment_returns,决策表本身不再存这些字段。
    entry_date: str | None = None
    entry_price: float | None = None
    entry_status: EntryStatus | None = None
    returns: dict[str, HorizonReturn] = Field(default_factory=dict)


class ExperimentListItem(ExperimentDecision):
    as_of: str
    data_cutoff_at: str


class ExperimentListResponse(BaseModel):
    items: list[ExperimentListItem]
    total: int
    page: int
    per_page: int


class ExperimentDetailResponse(BaseModel):
    run: ExperimentRun
    items: list[ExperimentDecision]
