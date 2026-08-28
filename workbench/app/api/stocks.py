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
    # 侧栏的全局批次选择器会带进这三个。不声明 FastAPI 会静默丢弃，
    # 用户切了批次却永远看到最新那一批——与 /api/experiments 曾经的 run_id 缺陷同源。
    run_id: str | None = Query(default=None, min_length=1, max_length=64),
    as_of: str | None = Query(default=None, min_length=8, max_length=8),
    strategy: str | None = Query(default=None, min_length=1, max_length=64),
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
        run_id=run_id,
        as_of=as_of,
        strategy=strategy,
    )


@router.get("/stocks/{ts_code}")
def stock_detail(ts_code: str, request: Request) -> dict:
    return StocksService(request.app.state.repository).detail(ts_code)
