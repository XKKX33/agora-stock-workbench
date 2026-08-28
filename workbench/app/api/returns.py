from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel

from app.dependencies import TS_CODE_PATTERN, get_returns_service
from app.schemas.experiments import EntryStatus
from engine.returns import HORIZONS

router = APIRouter()


class ReturnsCalculateRequest(BaseModel):
    run_id: str | None = None
    exchange: str = "SSE"



@router.post("/returns/calculate", status_code=status.HTTP_202_ACCEPTED)
def calculate_returns(
    body: ReturnsCalculateRequest | None = Body(default=None),
    run_id: Annotated[str | None, Query(min_length=1)] = None,
    exchange: Annotated[str, Query(min_length=1, max_length=16)] = "SSE",
    service=Depends(get_returns_service),
) -> dict:
    request = body or ReturnsCalculateRequest()
    return service.calculate(
        run_id=run_id or request.run_id,
        exchange=request.exchange if body else exchange,
    )


@router.get("/returns")
def list_returns(
    run_id: Annotated[str | None, Query(min_length=1)] = None,
    group_name: Annotated[str | None, Query(min_length=1)] = None,
    ts_code: Annotated[str | None, Query(pattern=TS_CODE_PATTERN)] = None,
    horizon: str | None = Query(
        default=None, pattern="^(" + "|".join(HORIZONS) + ")$"
    ),
    as_of: Annotated[str | None, Query(pattern=r"^[0-9]{8}$")] = None,
    entry_status: EntryStatus | None = None,
    service=Depends(get_returns_service),
) -> dict:
    return service.detail(
        run_id=run_id,
        group_name=group_name,
        ts_code=ts_code,
        horizon=horizon,
        as_of=as_of,
        entry_status=entry_status,
    )


@router.get("/returns/summary")
def returns_summary(
    run_id: Annotated[str | None, Query(min_length=1)] = None,
    as_of: Annotated[str | None, Query(pattern=r"^[0-9]{8}$")] = None,
    group_name: Annotated[str | None, Query(min_length=1)] = None,
    ts_code: Annotated[str | None, Query(pattern=TS_CODE_PATTERN)] = None,
    entry_status: EntryStatus | None = None,
    service=Depends(get_returns_service),
) -> dict:
    return service.summary(
        run_id=run_id,
        as_of=as_of,
        group_name=group_name,
        ts_code=ts_code,
        entry_status=entry_status,
    )
