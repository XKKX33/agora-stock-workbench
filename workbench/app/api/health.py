from fastapi import APIRouter, Depends

from app.dependencies import get_repository
from app.schemas.common import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(repository=Depends(get_repository)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        database=repository.health(),
        version="1.0.0",
    )
