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


def board_code(ts_code: Any) -> str:
    """从 ts_code 取交易所内的数字代码。

    为什么不取 snapshot 的 `symbol` 列:那一列来自 `LEFT JOIN stock_basic`,
    某只票没有 stock_basic 行时是 NaN。而 `ts_code` 是 join 的键,恒定存在。
    """
    return str(ts_code).split(".", 1)[0]


def is_mainboard(ts_code: Any) -> bool:
    """按 ts_code 判断是否主板。

    参数必须是完整 ts_code(如 `600000.SH`),不是 `symbol`。
    """
    return not board_code(ts_code).startswith(_NON_MAIN_PREFIXES)


def apply_universe(snap: pd.DataFrame, universe_cfg: Dict[str, Any]) -> pd.DataFrame:
    """按策略 universe 段过滤截面快照。返回新 DataFrame(不改原)。

    硬过滤的一条纪律:**判据缺失一律排除,不当成通过**。
    截面是 `daily LEFT JOIN stock_basic`,某票缺 stock_basic 行时 `name` 为
    NaN。旧实现 `name.fillna("")` 后判 ST,缺名字的票就"不含 ST"从而放行;
    板块判据取 NaN 的 `symbol`,`str(nan)="nan"` 也不以非主板前缀开头,同样
    放行。结果是一只信息不全、连板块都不知道的票静默进入候选池——比少一只
    候选严重得多。现在板块改从 ts_code 推,ST 判据缺失即排除。
    """
    df = snap.copy()
    if universe_cfg.get("exclude_st", True):
        # 名字缺失无法判断是否 ST,按排除处理。
        name = df["name"]
        df = df[name.notna() & ~name.astype(str).str.contains(r"ST|\*", regex=True)]
    price_max = universe_cfg.get("price_max")
    if price_max is not None:
        df = df[df["close"].astype(float) < float(price_max)]
    if universe_cfg.get("board", "mainboard") == "mainboard":
        df = df[df["ts_code"].map(is_mainboard)]
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
