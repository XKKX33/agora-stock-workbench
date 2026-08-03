"""题材/板块类因子：行业热度与龙头位置。"""

from __future__ import annotations

from .base import FactorSpec, factor
from .context import StockContext


@factor(FactorSpec("industry_heat", "theme", +1, "rank", "所属行业当日热度分"))
def industry_heat(ctx: StockContext) -> float:
    return ctx.get("industry_heat")


@factor(FactorSpec("industry_lead", "theme", -1, "rank", "行业热度排名(越小越靠前)", weight_in_cat=0.6))
def industry_lead(ctx: StockContext) -> float:
    # 排名越小越强，direction=-1 交给 normalize 处理方向
    return ctx.get("industry_rank")


@factor(FactorSpec("amount_top15pct", "theme", +1, "passthrough", "成交额是否居前15%(0/1)", weight_in_cat=0.4))
def amount_top15pct(ctx: StockContext) -> float:
    return ctx.get("amount_top15pct")
