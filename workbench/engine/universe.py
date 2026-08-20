"""候选池构建:硬性 universe 过滤 + 行业热度 + 候选种子排序。

迁移自 under70_strict_mainup_scan.py 的:
- code_prefix_filter (主板过滤)
- ST 过滤 / price_max / min_amount
- 行业热度 heat 及 top_inds
- seed_score 候选种子排序
保持口径一致,便于新旧引擎对拍。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

# 非主板前缀(创业板/科创板/北交所式)
_NON_MAIN_PREFIXES = ("300", "301", "688", "689", "8", "4", "9")

# 行业热度需要的最少成分股
_MIN_INDUSTRY_MEMBERS = 3
# 热门行业取前 N
_TOP_INDUSTRIES = 18
# 一线热门(线性加权更高)
_FIRST_TIER = 8


def is_mainboard(symbol: Any) -> bool:
    s = str(symbol)
    return not s.startswith(_NON_MAIN_PREFIXES)


def apply_universe(snap: pd.DataFrame, universe_cfg: Dict[str, Any]) -> pd.DataFrame:
    """按策略 universe 段过滤截面快照。返回新 DataFrame(不改原)。"""
    df = snap.copy()
    if universe_cfg.get("exclude_st", True):
        df = df[~df["name"].fillna("").str.contains(r"ST|\*", regex=True)]
    price_max = universe_cfg.get("price_max")
    if price_max is not None:
        df = df[df["close"].astype(float) < float(price_max)]
    if universe_cfg.get("board", "mainboard") == "mainboard":
        df = df[df["symbol"].map(is_mainboard)]
    min_amount_yi = universe_cfg.get("min_amount_yi")
    if min_amount_yi is not None:
        # Tushare amount 单位=千元;1 亿元 = 100000 千元
        df = df[df["amount"].astype(float) >= float(min_amount_yi) * 100000.0]
    return df.reset_index(drop=True)


def industry_heat(df: pd.DataFrame) -> pd.DataFrame:
    """行业热度表(与旧脚本一致的 heat 公式)。"""
    ind = (
        df.dropna(subset=["industry"])
        .groupby("industry")
        .agg(
            count=("ts_code", "count"),
            avg_pct=("pct_chg", "mean"),
            med_pct=("pct_chg", "median"),
            up_ratio=("pct_chg", lambda s: (s > 0).mean()),
            strong_ratio=("pct_chg", lambda s: (s >= 5).mean()),
            total_amount=("amount", "sum"),
        )
        .reset_index()
    )
    ind = ind[ind["count"] >= _MIN_INDUSTRY_MEMBERS]
    ind["heat"] = (
        ind["avg_pct"] * 0.25
        + ind["med_pct"] * 0.25
        + ind["up_ratio"] * 5
        + ind["strong_ratio"] * 10
        + np.log1p(ind["total_amount"]) / 5
    )
    return ind.sort_values("heat", ascending=False).reset_index(drop=True)


def industry_meta(ind: pd.DataFrame) -> Tuple[Dict[str, float], Dict[str, int], List[str]]:
    """返回 heat 映射、行业排名映射、热门行业列表。"""
    heat_map = dict(zip(ind["industry"], ind["heat"]))
    rank_map = {name: i + 1 for i, name in enumerate(ind["industry"].tolist())}
    top_inds = ind.head(_TOP_INDUSTRIES)["industry"].tolist()
    return heat_map, rank_map, top_inds


def build_candidates(
    df: pd.DataFrame, ind: pd.DataFrame, top_inds: List[str], limit: int
) -> pd.DataFrame:
    """候选种子:热门行业内领涨 + 全场领涨 + 大额,去重后按 seed 取 limit。"""
    parts = [
        df[df["industry"].isin(top_inds)].sort_values(
            ["pct_chg", "amount"], ascending=False
        ).head(220),
        df.sort_values(["pct_chg", "amount"], ascending=False).head(180),
        df.sort_values("amount", ascending=False).head(160),
    ]
    cand = pd.concat(parts).drop_duplicates("ts_code")
    cand = cand.copy()
    cand["seed_score"] = (
        cand["pct_chg"].fillna(0) * 2
        + np.log1p(cand["amount"].fillna(0)) / 2
    )
    return cand.sort_values("seed_score", ascending=False).head(limit).reset_index(drop=True)


def amount_top15pct_threshold(df: pd.DataFrame) -> float:
    """成交额 85 分位(用于 theme 的 amount_top15pct 因子)。"""
    return float(df["amount"].astype(float).quantile(0.85))
