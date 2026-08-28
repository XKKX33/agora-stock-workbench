"""Python 与本地 Pi Agent 服务之间的严格协议模型。

该模块只描述数据和结果校验，不负责网络、进程或数据库操作。所有哈希都由
同一套 canonical JSON 规则生成，避免 Python/Node 对输入排序不一致。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from engine.methodology import AGENT_ROLES, ANALYST_ROLES


class PiAgentValidationError(ValueError):
    """Pi 返回值不符合内部协议。"""


def normalize_hash(value: str) -> str:
    """接受纯 SHA-256 或 ``sha256:`` 前缀，并统一为小写纯十六进制。"""
    if not isinstance(value, str):
        raise ValueError("哈希必须是字符串")
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("哈希必须是 64 位 SHA-256 十六进制")
    return text


def _canonicalize(value: Any) -> Any:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonicalize(item) for key, item in value.items()}
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            _canonicalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("输入不能编码为严格 JSON") from exc

def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def compute_candidate_hash(candidates: list[dict[str, Any]]) -> str:
    return _sha256(candidates)


def compute_input_hash(
    candidates: list[dict[str, Any]], snapshots: list[dict[str, Any]]
) -> str:
    return _sha256({"candidates": candidates, "snapshots": snapshots})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PiLimits(_StrictModel):
    coarse: int = Field(ge=1, le=20)
    deep: int = Field(ge=1, le=20)
    final: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def validate_order(self) -> "PiLimits":
        if self.deep > self.coarse:
            raise ValueError("deep 不可大于 coarse")
        if self.final > self.deep:
            raise ValueError("final 不可大于 deep")
        return self


class PiModelConfig(_StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int = Field(ge=1, le=32768)


class PiMethodology(_StrictModel):
    """随请求下发的方法论载荷，由 engine.methodology 装配。

    Pi Agent 只拥有输出 JSON schema；方法论正文与角色职责一律以 Python 为准，
    避免两侧各自维护一份提示词。
    """

    text: str = Field(min_length=1)
    role_briefs: dict[str, str]

    @model_validator(mode="after")
    def validate_roles(self) -> "PiMethodology":
        missing = [role for role in AGENT_ROLES if not self.role_briefs.get(role, "").strip()]
        if missing:
            raise ValueError(f"role_briefs 缺少角色职责: {missing}")
        unknown = sorted(set(self.role_briefs) - set(AGENT_ROLES))
        if unknown:
            raise ValueError(f"role_briefs 含未声明角色: {unknown}")
        return self


class PiAgentRequest(_StrictModel):
    protocol_version: Literal["1"]
    workflow_version: Literal["1"]
    mode: Literal["batch", "single"]
    trade_date: str = Field(min_length=1)
    candidate_hash: str
    input_hash: str
    limits: PiLimits
    candidates: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    snapshots: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    model: PiModelConfig
    methodology: PiMethodology

    @field_validator("candidate_hash", "input_hash", mode="before")
    @classmethod
    def _normalize_hash(cls, value: str) -> str:
        return normalize_hash(value)

    @model_validator(mode="after")
    def validate_frozen_input(self) -> "PiAgentRequest":
        candidate_codes = [_code(item) for item in self.candidates]
        if any(not code for code in candidate_codes):
            raise ValueError("candidates 中每项都必须包含 ts_code")
        if len(set(candidate_codes)) != len(candidate_codes):
            raise ValueError("candidates 的 ts_code 不可重复")
        snapshot_codes = [_code(item) for item in self.snapshots]
        if any(not code for code in snapshot_codes):
            raise ValueError("snapshots 中每项都必须包含 ts_code")
        if len(set(snapshot_codes)) != len(snapshot_codes):
            raise ValueError("snapshots 的 ts_code 不可重复")
        if not set(snapshot_codes) <= set(candidate_codes):
            raise ValueError("snapshots 必须属于 frozen_pool")
        expected_candidate = compute_candidate_hash(self.candidates)
        expected_input = compute_input_hash(self.candidates, self.snapshots)
        if self.candidate_hash != expected_candidate:
            raise ValueError("candidate_hash 与冻结候选不一致")
        if self.input_hash != expected_input:
            raise ValueError("input_hash 与冻结输入不一致")
        return self


class PiAnalyst(_StrictModel):
    stance: Literal["bull", "bear", "neutral"]
    conclusion: str = Field(min_length=1)
    risks: list[str]


class PiCoarseItem(_StrictModel):
    ts_code: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float
    reason: str = Field(min_length=1)

    @field_validator("score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("score 必须是 0~100 的有限数字")
        return value


class PiDeepItem(_StrictModel):
    ts_code: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float
    analysts: dict[str, PiAnalyst]

    @field_validator("score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("score 必须是 0~100 的有限数字")
        return value

    @model_validator(mode="after")
    def _analyst_roles(self) -> "PiDeepItem":
        if set(self.analysts) != set(ANALYST_ROLES):
            raise ValueError("analysts 必须包含 " + "/".join(ANALYST_ROLES))
        return self


class PiFinalItem(_StrictModel):
    ts_code: str = Field(min_length=1)
    rank: int = Field(ge=1)
    decision: str = Field(min_length=1)
    score: float
    reason: str | None = None
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    rebuttal: str = Field(min_length=1)
    risk_control: str = Field(min_length=1)


class PiFinalPick(_StrictModel):
    """最终决策人对每只入选股的选择记录。"""

    ts_code: str = Field(min_length=1)
    rank: int = Field(ge=1)
    reason: str = Field(min_length=1)


class PiUsage(_StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


PiRunState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class PiHealthResponse(_StrictModel):
    protocol_version: Literal["1"]
    pi_version: str | None = None
    workflow_version: Literal["1"] | None = None
    running: bool | None = None
    status: str | None = None


class PiRunResponse(_StrictModel):
    run_id: str = Field(min_length=1)
    status: PiRunState
    error: str | None = None


class PiJudgmentResult(_StrictModel):
    protocol_version: Literal["1"]
    workflow_version: Literal["1"]
    run_id: str = Field(min_length=1)
    trade_date: str = Field(min_length=1)
    candidate_hash: str
    input_hash: str
    coarse: list[PiCoarseItem]
    deep: list[PiDeepItem]
    final: list[PiFinalItem]
    picks: list[PiFinalPick] = Field(default_factory=list)
    usage: PiUsage | None = None

    @field_validator("candidate_hash", "input_hash", mode="before")
    @classmethod
    def _normalize_hash(cls, value: str) -> str:
        return normalize_hash(value)


def _code(item: dict[str, Any]) -> str:
    value = item.get("ts_code")
    return str(value).strip().upper() if value is not None else ""


def _validate_stage(items: list[Any], stage: str, frozen: set[str]) -> set[str]:
    codes = [item.ts_code.strip().upper() for item in items]
    if len(set(codes)) != len(codes):
        raise PiAgentValidationError(f"{stage} 的 ts_code 不可重复")
    if not set(codes) <= frozen:
        raise PiAgentValidationError(f"{stage} 必须是 frozen_pool 的 subset")
    if [item.rank for item in items] != list(range(1, len(items) + 1)):
        raise PiAgentValidationError(f"{stage} rank 必须连续从 1 开始")
    return set(codes)


def validate_judgment_result(
    value: dict[str, Any], request: PiAgentRequest, run_id: str
) -> PiJudgmentResult:
    """将 Pi 原始 JSON 解析为模型，并验证跨阶段集合关系和数量。"""
    try:
        result = PiJudgmentResult.model_validate(value)
    except ValidationError as exc:
        # 只保留字段路径和固定原因，绝不把模型原始值写入日志或数据库。
        details: list[str] = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or "result"
            error_type = str(item.get("type", "invalid"))
            if error_type == "missing":
                reason = "缺少字段"
            elif "score" in location:
                reason = "score 必须是 0~100 的有限数字"
            elif "analysts" in location:
                reason = "analysts 字段无效"
            else:
                reason = "字段无效"
            details.append(f"{location}: {reason}")
        raise PiAgentValidationError("结果结构无效: " + "; ".join(details)) from exc
    except Exception as exc:  # noqa: BLE001 - defensive conversion at protocol boundary
        raise PiAgentValidationError("结果结构无效") from exc
    if result.run_id != run_id:
        raise PiAgentValidationError("run_id 与请求不一致")
    if result.trade_date != request.trade_date:
        raise PiAgentValidationError("trade_date 与冻结输入不一致")
    if result.candidate_hash != request.candidate_hash:
        raise PiAgentValidationError("candidate_hash 与请求不一致")
    if result.input_hash != request.input_hash:
        raise PiAgentValidationError("input_hash 与请求不一致")
    frozen = {_code(item) for item in request.candidates}
    coarse = _validate_stage(result.coarse, "coarse", frozen)
    deep = _validate_stage(result.deep, "deep", coarse)
    final = _validate_stage(result.final, "final", deep)
    # 最终决策人的 picks 必须指向 final 里已有的股票,不许凭空出现。
    final_codes = {item.ts_code for item in result.final}
    pick_codes: set[str] = set()
    previous_rank = 0
    for pick in result.picks:
        if pick.ts_code not in final_codes:
            raise PiAgentValidationError(f"picks 引用了 final 之外的股票: {pick.ts_code}")
        if pick.ts_code in pick_codes:
            raise PiAgentValidationError(f"picks 重复引用 {pick.ts_code}")
        pick_codes.add(pick.ts_code)
        if pick.rank != previous_rank + 1:
            raise PiAgentValidationError("picks rank 必须从 1 连续递增")
        previous_rank = pick.rank
    if request.mode == "batch":
        limits = request.limits
        if len(result.coarse) > limits.coarse:
            raise PiAgentValidationError("batch 的 coarse 数量不可超过 coarse 上限")
        if len(result.deep) > limits.deep:
            raise PiAgentValidationError("batch 的 deep 数量不可超过 deep 上限")
        if len(result.final) > limits.final:
            raise PiAgentValidationError("batch 的 final 数量不可超过 final 上限")
    elif any(count > 1 for count in (len(result.coarse), len(result.deep), len(result.final))):
        raise PiAgentValidationError("single 每个阶段最多返回 1 只")
    return result

__all__ = [
    "PiAgentRequest", "PiAgentValidationError", "PiAnalyst", "PiCoarseItem",
    "PiDeepItem", "PiFinalItem", "PiJudgmentResult", "PiLimits", "PiMethodology",
    "PiModelConfig", "PiUsage", "compute_candidate_hash", "compute_input_hash",
    "normalize_hash", "validate_judgment_result", "PiHealthResponse", "PiRunResponse",
    "PiRunState",
]
