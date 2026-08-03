"""资金类因子（事后确认）。

资金流是 T 日尾盘/次日才确认的滞后信号，作为"叠加确认"而非前置选股条件。
这些因子登记入册，主要服务于自动复盘与后续 ML 特征；
打分时资金贡献由 score 层按 money_class 的 overlay 映射注入，避免重复计。
"""

from __future__ import annotations

import math

from .base import FactorSpec, factor
from .context import StockContext


def classify_money(net5: float, big5: float) -> str:
    """资金分层判定（迁移自旧脚本 classify_money，语义一致）。

    net5: 近5日总净流入；big5: 近5日大单+超大单净额。
    输出为 config.MONEY_CLASSES 中的权威键，供 score 层 overlay 映射。
    """
    if net5 is None or big5 is None or (isinstance(net5, float) and math.isnan(net5)) \
            or (isinstance(big5, float) and math.isnan(big5)):
        return "资金未充分确认"
    if net5 > 0 and big5 > 0:
        return "资金一致确认"
    if net5 < 0 and big5 > 0:
        return "大资金承接型强分歧"
    if net5 > 0 and big5 <= 0:
        return "总资金认可但大单不连续"
    if net5 < 0 and big5 < 0:
        return "资金同步分歧，降级"
    return "资金未充分确认"


@factor(FactorSpec("net5", "money", +1, "zscore", "近5日净流入合计", weight_in_cat=0.0))
def net5(ctx: StockContext) -> float:
    return ctx.money.get("net5", float("nan"))


@factor(FactorSpec("big5", "money", +1, "zscore", "近5日大单+超大单净额", weight_in_cat=0.0))
def big5(ctx: StockContext) -> float:
    return ctx.money.get("big5", float("nan"))


@factor(FactorSpec("big_pos_days", "money", +1, "passthrough", "近5日大单净流入天数", weight_in_cat=0.0))
def big_pos_days(ctx: StockContext) -> float:
    return ctx.money.get("big_pos_days", float("nan"))
