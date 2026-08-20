from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_experiment_service
from app.errors import WorkbenchError
from app.schemas.experiments import (
    EntryStatus,
    ExperimentDetailResponse,
    ExperimentGroup,
    ExperimentListResponse,
)


router = APIRouter()


def validated_signal_date(
    as_of: Annotated[str | None, Query(pattern=r"^[0-9]{8}$")] = None,
) -> str | None:
    if as_of is None:
        return None
    try:
        datetime.strptime(as_of, "%Y%m%d")
    except ValueError as exc:
        raise WorkbenchError(
            "request_validation_failed",
            "as_of 必须是真实存在的 YYYYMMDD 日期",
            status_code=422,
            details={"field": "as_of"},
        ) from exc
    return as_of


@router.get("/experiments", response_model=ExperimentListResponse)
def list_experiments(
    as_of: str | None = Depends(validated_signal_date),
    group: ExperimentGroup | None = None,
    ts_code: Annotated[
        str | None, Query(pattern=r"^[0-9]{6}\.(SZ|SH|BJ)$")
    ] = None,
    entry_status: EntryStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    service=Depends(get_experiment_service),
) -> dict:
    return service.list(
        as_of=as_of,
        group_name=group,
        ts_code=ts_code,
        entry_status=entry_status,
        page=page,
        per_page=per_page,
    )


@router.get("/experiments/{run_id}", response_model=ExperimentDetailResponse)
def experiment_detail(run_id: str, service=Depends(get_experiment_service)) -> dict:
    return service.detail(run_id)
