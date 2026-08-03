"""结构类因子：波浪位置、突破、均线多头排列。

对应旧脚本 wave_score 的可分解成分，但拆成独立因子，
便于横截面归一化与单独归因。
"""

from __future__ import annotations

from .base import FactorSpec, factor
from .context import StockContext


@factor(FactorSpec("pos60", "structure", +1, "rank", "60日区间位置(0-1)"))
def pos60(ctx: StockContext) -> float:
    return ctx.get("pos60")


@factor(FactorSpec("breakout", "structure", +1, "rank", "新高突破强度(0/1/2)"))
def breakout(ctx: StockContext) -> float:
    return ctx.get("breakout")


@factor(FactorSpec("trend_combo", "structure", +1, "rank", "中期趋势组合强度(0/1/2)"))
def trend_combo(ctx: StockContext) -> float:
    return ctx.get("trend_combo")


@factor(FactorSpec("ma_stack", "structure", +1, "rank", "均线多头排列(0-4)"))
def ma_stack(ctx: StockContext) -> float:
    return ctx.get("ma_stack")
