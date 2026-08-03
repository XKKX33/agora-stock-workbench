"""AI 复盘服务。

只做两件事:上报可用性;在可用时基于数据库事实生成叙述。未配置时明确
返回 unconfigured,调用生成接口会拿到 503——不返回任何编造内容。
"""

from __future__ import annotations

from typing import Optional

from engine.ai import AIUnavailableError, describe, load_ai_config, narrate_review
from engine.config import load_settings

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository
from app.services.reviews import ReviewService


class AIService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository
        self.config = load_ai_config(load_settings())

    def status(self) -> dict:
        """AI 可用性。页面据此显示"未配置"而不是留白。"""
        return describe(self.config)

    def narrate(
        self, trade_date: Optional[str] = None, strategy: Optional[str] = None
    ) -> dict:
        """基于已入库事实生成盘后复盘叙述。

        未配置时抛 503 而不是回退到规则模板:模板输出看起来和模型输出一样,
        用户没有任何办法分辨自己看到的是哪一个。
        """
        review = ReviewService(self.repository).get(
            trade_date=trade_date, strategy=strategy
        )
        try:
            narrative = narrate_review(self.config, review)
        except AIUnavailableError as error:
            raise WorkbenchError(
                "ai_unavailable", str(error), status_code=503,
                details=self.status(),
            ) from error
        return {
            "trade_date": review["trade_date"],
            "strategy": review["strategy"],
            "provider": self.config.provider,
            "model": self.config.model,
            "narrative": narrative,
            # 引用锚点:叙述只能重述这些小节,页面可据此回链到原始事实
            "grounded_in": review["available_sections"],
            "missing": review["missing"],
        }
