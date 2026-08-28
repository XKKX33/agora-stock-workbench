from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies import (
    TS_CODE_PATTERN,
    get_experiment_service,
    validated_signal_date,
)
from app.schemas.experiments import (
    EntryStatus,
    ExperimentDetailResponse,
    ExperimentGroup,
    ExperimentListResponse,
)


router = APIRouter()


@router.get("/experiments", response_model=ExperimentListResponse)
def list_experiments(
    as_of: str | None = Depends(validated_signal_date),
    # 总览页选定批次后会把 run_id 带进台账页。不声明它 FastAPI 会静默丢弃,
    # 用户看到的是同一信号日下**所有批次混合**的列表,同一只票出现多次,
    # 却以为在看自己选的那一个批次。
    run_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    group: ExperimentGroup | None = None,
    ts_code: Annotated[str | None, Query(pattern=TS_CODE_PATTERN)] = None,
    entry_status: EntryStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    service=Depends(get_experiment_service),
) -> dict:
    return service.list(
        as_of=as_of,
        run_id=run_id,
        group_name=group,
        ts_code=ts_code,
        entry_status=entry_status,
        page=page,
        per_page=per_page,
    )


@router.get("/experiments/batches")
def experiment_batches(service=Depends(get_experiment_service)) -> dict:
    """已落库批次列表，供台账的批次下拉框使用。

    路由顺序说明:这条必须排在 /experiments/{run_id} 之前,否则 "batches"
    会被当成 run_id 匹配掉,返回 404。
    """
    return service.batches()


@router.get("/experiments/{run_id}", response_model=ExperimentDetailResponse)
def experiment_detail(run_id: str, service=Depends(get_experiment_service)) -> dict:
    return service.detail(run_id)
