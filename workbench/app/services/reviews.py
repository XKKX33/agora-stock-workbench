"""复盘读服务。

只读装配:调 build_review 时固定 backfill=False。打开页面看一眼复盘不该
悄悄回填 retN——回填是盘后链条的显式步骤,不是页面渲染的副作用。

日期口径:默认日期取可见日(基准日往前退 N 个开市日),不是行情最新交易日。
复盘要拿"当时能看到的信息"去对照后续走势,用最新交易日复盘等于把还没落地的
收益提前当成结论。显式日期必须 <= 可见日,落在隐藏窗口内直接拒绝。
"""

from __future__ import annotations

from typing import Optional

from engine.config import load_settings
from engine.db import Store
from engine.visibility import (
    LookaheadBlocked,
    ensure_visible,
    require_visible_as_of,
    resolve_window,
)

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository

# 交易日历口径:全项目统一按上交所日历推可见窗口。
EXCHANGE = "SSE"


class ReviewService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def get(
        self, trade_date: Optional[str] = None, strategy: Optional[str] = None
    ) -> dict:
        """某交易日的复盘。trade_date 省略时取可见日。

        可见窗口算不出来说明库里连一个可复盘的交易日都没有,这是"读不到"而不是
        "读出来是空的",沿用 no_trade_date 直接报错,并把真实原因带出去,
        而不是返回一份全 available=False 的复盘。
        """
        window = self._window()
        if trade_date is None:
            try:
                target = require_visible_as_of(window)
            except LookaheadBlocked as exc:
                raise WorkbenchError(
                    "no_trade_date",
                    f"无法确定复盘日期:{exc}",
                    status_code=404,
                    details=window.as_dict(),
                ) from exc
        else:
            try:
                target = ensure_visible(str(trade_date), window)
            except LookaheadBlocked as exc:
                raise WorkbenchError(
                    exc.code,
                    str(exc),
                    status_code=400 if exc.code == "lookahead_blocked" else 409,
                    details=window.as_dict(),
                ) from exc
        return self.repository.review(trade_date=str(target), strategy=strategy)

    def _window(self):
        """按本地基准日算可见窗口。复盘只读本地历史,不联网确认基准日。"""
        self.repository.ensure_database()
        with Store(self.repository.db_path, ensure_schema=False) as store:
            return resolve_window(store, load_settings(), exchange=EXCHANGE)
