"""自选股路由。"""

from typing import Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel

from app.services.watchlist import WatchlistService

router = APIRouter()


class WatchlistAddRequest(BaseModel):
    ts_code: str
    note: str | None = None


@router.get("/watchlist")
def watchlist_list(
    request: Request,
    search: str | None = None,
    industry: str | None = None,
    sort: Literal["sort_order", "name", "industry", "close", "pct_chg"] = "sort_order",
    order: Literal["asc", "desc"] = "asc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> dict:
    return WatchlistService(request.app.state.repository).list(
        search=search,
        industry=industry,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
    )


@router.post("/watchlist")
def watchlist_add(
    request: Request,
    body: WatchlistAddRequest = Body(...),
) -> dict:
    return WatchlistService(request.app.state.repository).add(
        body.ts_code, body.note
    )


@router.delete("/watchlist/{ts_code}")
def watchlist_remove(ts_code: str, request: Request) -> dict:
    return WatchlistService(request.app.state.repository).remove(ts_code)
