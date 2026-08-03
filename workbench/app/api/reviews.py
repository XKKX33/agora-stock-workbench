from fastapi import APIRouter, Depends

from app.dependencies import get_repository
from app.services.reviews import ReviewService

router = APIRouter()


@router.get("/reviews")
def get_review(
    trade_date: str | None = None,
    strategy: str | None = None,
    repository=Depends(get_repository),
) -> dict:
    """收盘后复盘。每一节要么 available=True 带 data,要么带 missing_reason。

    这是纯读接口:内部固定 backfill=False,不会因为刷新页面而回填 retN。
    需要回填走 POST /api/pipelines(盘后链条第 3 步)。
    """
    return ReviewService(repository).get(trade_date=trade_date, strategy=strategy)
