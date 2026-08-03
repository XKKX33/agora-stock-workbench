"""量能类因子。

vol_health 用"甜蜜区间"打分而非原始放量比，避免爆量出货被误判为强。
vol5_20/amt5_20 作为辅助排序信号保留。
"""

from __future__ import annotations

from .base import FactorSpec, factor
from .context import StockContext


@factor(FactorSpec("vol_health", "volume", +1, "passthrough", "量能健康度(0-4,甜蜜区间)"))
def vol_health(ctx: StockContext) -> float:
    return ctx.get("vol_health")


@factor(FactorSpec("amt5_20", "volume", +1, "rank", "5日/20日成交额比", weight_in_cat=0.5))
def amt5_20(ctx: StockContext) -> float:
    return ctx.get("amt5_20")
