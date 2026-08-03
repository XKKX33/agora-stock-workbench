"""MACD 类因子：日线与周线多头强度。"""

from __future__ import annotations

from .base import FactorSpec, factor
from .context import StockContext


@factor(FactorSpec("macd_bull", "macd", +1, "passthrough", "日线MACD多头计数(0-4)"))
def macd_bull(ctx: StockContext) -> float:
    return ctx.get("macd_bull")


@factor(FactorSpec("weekly_bull", "macd", +1, "passthrough", "周线MACD多头计数(0-4)"))
def weekly_bull(ctx: StockContext) -> float:
    return ctx.get("weekly_bull")
