"""全市场筛选路由。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import validated_signal_date
from app.services.screener import ScreenerService

router = APIRouter()


@router.get("/screener")
def screener(
    request: Request,
    pct_min: float | None = None,
    pct_max: float | None = None,
    vol_ratio_min: float | None = None,
    industry: str | None = None,
    sort: Literal["close", "pct_chg", "vol_ratio", "turnover_rate", "rsi6", "total_mv", "circ_mv"] = "pct_chg",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=200),
    run_id: str | None = None,
    as_of: str | None = Depends(validated_signal_date),
    strategy: str | None = None,
) -> dict:
    return ScreenerService(request.app.state.repository.db_path).list(
        pct_min=pct_min, pct_max=pct_max, vol_ratio_min=vol_ratio_min, industry=industry,
        sort=sort, order=order, page=page, per_page=per_page,
        run_id=run_id, as_of=as_of, strategy=strategy,
    )
