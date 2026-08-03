"""复盘读服务。

只读装配:调 build_review 时固定 backfill=False。打开页面看一眼复盘不该
悄悄回填 retN——回填是盘后链条的显式步骤,不是页面渲染的副作用。
"""

from __future__ import annotations

from typing import Optional

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository


class ReviewService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def get(
        self, trade_date: Optional[str] = None, strategy: Optional[str] = None
    ) -> dict:
        """某交易日的复盘。trade_date 省略时取行情最新交易日。

        取不到最新交易日说明库里一根日线都没有,这是"读不到"而不是
        "读出来是空的",直接报错而不是返回一份全 available=False 的复盘。
        """
        target = trade_date or self.repository.latest_trade_date()
        if not target:
            raise WorkbenchError(
                "no_trade_date",
                "数据库中还没有任何行情数据,无法确定复盘日期",
                status_code=404,
            )
        return self.repository.review(trade_date=str(target), strategy=strategy)
