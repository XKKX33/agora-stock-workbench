from fastapi import APIRouter, Request

from app.services.overview import OverviewService

router = APIRouter()


@router.get("/overview")
def overview(request: Request) -> dict:
    service = OverviewService(
        request.app.state.repository,
        request.app.state.scan_manager,
    )
    return service.get()
