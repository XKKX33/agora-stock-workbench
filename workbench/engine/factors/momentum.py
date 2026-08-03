"""动量类因子：纯收益动量，不含结构位置。"""

from __future__ import annotations

from .base import FactorSpec, factor
from .context import StockContext


@factor(FactorSpec("ret20", "momentum", +1, "rank", "近20日涨幅"))
def ret20(ctx: StockContext) -> float:
    return ctx.get("ret20")


@factor(FactorSpec("ret60", "momentum", +1, "rank", "近60日涨幅"))
def ret60(ctx: StockContext) -> float:
    return ctx.get("ret60")


@factor(FactorSpec("pct_chg", "momentum", +1, "rank", "当日涨幅", weight_in_cat=0.5))
def pct_chg(ctx: StockContext) -> float:
    return ctx.get("pct_chg")
