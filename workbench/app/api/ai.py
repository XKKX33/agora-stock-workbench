from fastapi import APIRouter, Depends

from app.dependencies import get_repository
from app.services.ai import AIService

router = APIRouter()


@router.get("/ai/status")
def ai_status(repository=Depends(get_repository)) -> dict:
    """AI 可用性。availability 为 disabled / unconfigured / available 之一。

    未配置时 missing 会列出到底缺什么(provider / model / 凭据环境变量),
    页面据此显示"未配置",不留白也不编内容。
    """
    return AIService(repository).status()


@router.post("/ai/reviews")
def ai_review(
    trade_date: str | None = None,
    strategy: str | None = None,
    repository=Depends(get_repository),
) -> dict:
    """基于数据库事实生成盘后复盘叙述。

    未配置凭据时返回 503 + ai_unavailable,不返回任何编造文本。
    """
    return AIService(repository).narrate(trade_date=trade_date, strategy=strategy)
