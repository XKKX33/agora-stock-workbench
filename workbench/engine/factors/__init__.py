"""因子包：导入各模块以触发 @factor 注册。

使用方：
    from engine.factors import FACTORS, FactorSpec, StockContext, build_context
"""

from __future__ import annotations

from .base import (  # noqa: F401
    CATEGORIES,
    FACTORS,
    FactorSpec,
    clear_registry,
    factor,
    factors_by_category,
)
from .context import StockContext, build_context, macd  # noqa: F401

# 导入副作用：注册所有因子
from . import momentum  # noqa: F401,E402
from . import structure  # noqa: F401,E402
from . import macd_factors  # noqa: F401,E402
from . import volume  # noqa: F401,E402
from . import money  # noqa: F401,E402
from . import theme  # noqa: F401,E402
