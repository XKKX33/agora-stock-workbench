from fastapi import APIRouter, Query, Request

from app.services.analytics import AnalyticsService

router = APIRouter()


@router.get("/sentiment")
def sentiment(request: Request) -> dict:
    return AnalyticsService(request.app.state.repository).sentiment()


@router.get("/factors")
def factors(request: Request) -> dict:
    return AnalyticsService(request.app.state.repository).factors()


@router.get("/factors/{ts_code}")
def factor_detail(ts_code: str, request: Request) -> dict:
    return AnalyticsService(request.app.state.repository).factor_detail(ts_code)


@router.get("/ledger")
def ledger(
    request: Request,
    strategy: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> dict:
    return AnalyticsService(request.app.state.repository).ledger(
        strategy, page, per_page
    )


@router.get("/ledger/summary")
def ledger_summary(request: Request, strategy: str | None = None) -> dict:
    return AnalyticsService(request.app.state.repository).ledger_summary(strategy)
