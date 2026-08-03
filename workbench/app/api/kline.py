"""行情K线路由。"""

from fastapi import APIRouter, Query, Request

from app.services.kline import KlineService

router = APIRouter()


@router.get("/kline/search")
def kline_search(
    request: Request,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return KlineService(request.app.state.repository.db_path).search(q=q, limit=limit)


@router.get("/kline/{ts_code}")
def kline_detail(
    ts_code: str,
    request: Request,
    days: int = Query(default=250, ge=1, le=1000),
) -> dict:
    service = KlineService(request.app.state.repository.db_path)
    quote = service.quote(ts_code)
    kline = service.kline(ts_code, days=days)
    return {**quote, "bars": kline["bars"]}
