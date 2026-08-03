"""横截面归一化层。

把每个因子的原始值，在"当日候选池"内归一化到 [0,1] 强度空间，
方向(direction)统一在这里处理。这样不同量纲的因子可加权比较，
彻底取代旧脚本里散落的魔法乘数。

约定：
- 输出恒为 [0,1]，1 表示该维度最强。
- 缺失值(NaN)归为中性 0.5，避免缺数据被误判为弱。
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from .factors import FACTORS
from .factors.context import StockContext

NEUTRAL = 0.5


def evaluate_factors(contexts: List[StockContext]) -> pd.DataFrame:
    """对每个 context 求出所有因子原始值。index=ts_code。"""
    records = {}
    for ctx in contexts:
        row = {}
        for name, (_spec, fn) in FACTORS.items():
            try:
                row[name] = float(fn(ctx))
            except Exception:
                row[name] = np.nan
        records[ctx.ts_code] = row
    return pd.DataFrame.from_dict(records, orient="index")


def _rank01(s: pd.Series, direction: int) -> pd.Series:
    r = s.rank(pct=True, method="average")
    if direction < 0:
        r = 1.0 - r
    return r


def _zscore01(s: pd.Series, direction: int) -> pd.Series:
    mu = s.mean()
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd < 1e-12:
        return pd.Series(NEUTRAL, index=s.index)
    z = (s - mu) / sd
    if direction < 0:
        z = -z
    return ((z + 3.0) / 6.0).clip(0.0, 1.0)


def _minmax01(s: pd.Series, direction: int) -> pd.Series:
    lo, hi = s.min(), s.max()
    if not np.isfinite(hi - lo) or (hi - lo) < 1e-12:
        return pd.Series(NEUTRAL, index=s.index)
    m = (s - lo) / (hi - lo)
    if direction < 0:
        m = 1.0 - m
    return m


def normalize_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """按各因子 spec 归一化为 [0,1] 强度。列缺失自动跳过。"""
    out = {}
    for name, (spec, _fn) in FACTORS.items():
        if name not in raw.columns:
            continue
        col = raw[name].astype(float)
        valid = col.dropna()
        if valid.empty:
            out[name] = pd.Series(NEUTRAL, index=raw.index)
            continue
        if spec.normalize == "rank":
            norm = _rank01(col, spec.direction)
        elif spec.normalize == "zscore":
            norm = _zscore01(col, spec.direction)
        else:  # passthrough -> 池内 min-max
            norm = _minmax01(col, spec.direction)
        out[name] = norm.fillna(NEUTRAL)
    return pd.DataFrame(out, index=raw.index)
