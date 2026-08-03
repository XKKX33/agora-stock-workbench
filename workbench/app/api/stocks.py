from typing import Literal

from fastapi import APIRouter, Query, Request

from app.services.stocks import StocksService

router = APIRouter()


@router.get("/stocks")
def stocks(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=200),
    passed: bool | None = None,
    selected: bool | None = None,
    industry: str | None = None,
    search: str | None = None,
    sort: Literal["rank", "total", "industry", "money_class"] = "rank",
    order: Literal["asc", "desc"] = "asc",
) -> dict:
    return StocksService(request.app.state.repository).list(
        page=page,
        per_page=per_page,
        passed=passed,
        selected=selected,
        industry=industry,
        search=search,
        sort=sort,
        order=order,
    )


@router.get("/stocks/{ts_code}")
def stock_detail(ts_code: str, request: Request) -> dict:
    return StocksService(request.app.state.repository).detail(ts_code)
