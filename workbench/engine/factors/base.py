"""因子注册表。

设计铁律：
1. 因子是纯函数：输入一个 StockContext，输出一个 float 原始值（或 NaN）。
2. 因子自身不做归一化、不做加权、不看别的股票——横截面处理由 normalize 层负责。
3. 每个因子声明元数据：类别、方向、归一化方式、中文释义。
4. 新增因子只需 @factor 装饰，无需改动打分框架。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Tuple

# 允许的类别（对应策略权重桶）
CATEGORIES = ("momentum", "structure", "macd", "volume", "money", "theme")

# 允许的归一化方式
NORMALIZERS = ("rank", "zscore", "passthrough")


@dataclass(frozen=True)
class FactorSpec:
    """因子元数据。"""

    name: str
    category: str          # momentum/structure/macd/volume/money/theme
    direction: int         # +1: 越大越强; -1: 越小越强
    normalize: str         # rank/zscore/passthrough
    desc_cn: str
    weight_in_cat: float = 1.0  # 同类别内相对权重（默认等权）

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"未知因子类别: {self.category} (name={self.name})")
        if self.direction not in (1, -1):
            raise ValueError(f"direction 必须为 +1/-1: {self.direction} (name={self.name})")
        if self.normalize not in NORMALIZERS:
            raise ValueError(f"未知归一化方式: {self.normalize} (name={self.name})")


# 全局注册表: name -> (spec, fn)
FACTORS: Dict[str, Tuple[FactorSpec, Callable]] = {}


def factor(spec: FactorSpec):
    """装饰器：把纯函数因子注册进 FACTORS。"""

    def _wrap(fn: Callable):
        if spec.name in FACTORS:
            raise ValueError(f"因子重复注册: {spec.name}")
        FACTORS[spec.name] = (spec, fn)
        return fn

    return _wrap


def factors_by_category(category: str) -> Dict[str, Tuple[FactorSpec, Callable]]:
    return {k: v for k, v in FACTORS.items() if v[0].category == category}


def clear_registry() -> None:
    """仅供测试使用：清空注册表。"""
    FACTORS.clear()
