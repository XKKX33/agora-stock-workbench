"""打分层：raw → 归一化 → 类别分 → 门槛 → 可解释总分。

总分构成（完全可加、可归因）：
    total = Σ_类别 (W_类别 · 类别分) + 资金overlay
    类别分 = Σ_因子 (w_因子 / Σw) · norm_因子      ∈ [0,1]
    contrib_因子 = W_类别 · (w_因子 / Σw) · norm_因子

资金(money)不进类别加权，而是按 money_class 走 overlay 增量，
与旧脚本"资金事后叠加确认"的语义一致，避免重复计分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .factors import FACTORS
from .factors.context import StockContext
from .gates import passes_gates
from .normalize import evaluate_factors, normalize_frame

# 门槛需要的原始特征键
_GATE_FEAT_KEYS = ("ret20", "macd_bull", "weekly_bull", "vol_health", "pct_chg")

# 类别 -> 中文名（用于一句话归因）
_CAT_CN = {
    "theme": "题材",
    "structure": "结构",
    "momentum": "动量",
    "macd": "MACD",
    "volume": "量能",
    "money": "资金",
}


@dataclass
class ScoredStock:
    ts_code: str
    name: str
    industry: str
    total: float
    passed: bool
    gate_reasons: List[str]
    cat_scores: Dict[str, float]
    contrib: Dict[str, float]          # 每因子对总分的贡献
    money_class: Optional[str]
    one_line: str
    feat: Dict[str, float] = field(default_factory=dict)


def _category_scores(norm: pd.DataFrame) -> pd.DataFrame:
    """把归一化因子聚合为类别分。返回 index=ts_code, 列=cat_<类别>。"""
    cats: Dict[str, pd.Series] = {}
    contrib_weight: Dict[str, Dict[str, float]] = {}  # 类别内各因子权重占比
    for cat in {spec.category for spec, _ in FACTORS.values()}:
        members = {
            name: spec.weight_in_cat
            for name, (spec, _fn) in FACTORS.items()
            if spec.category == cat and spec.weight_in_cat > 0 and name in norm.columns
        }
        if not members:
            continue
        wsum = sum(members.values())
        acc = pd.Series(0.0, index=norm.index)
        share = {}
        for name, w in members.items():
            share[name] = w / wsum
            acc = acc + norm[name] * share[name]
        cats[f"cat_{cat}"] = acc
        contrib_weight[cat] = share
    frame = pd.DataFrame(cats, index=norm.index)
    frame.attrs["contrib_weight"] = contrib_weight
    return frame


def score_pool(
    contexts: List[StockContext],
    strategy: Dict,
) -> List[ScoredStock]:
    """对候选池打分。strategy 需含 weights / gates / money_overlay。"""
    if not contexts:
        return []

    weights: Dict[str, float] = dict(strategy.get("weights", {}))
    gate_cfg: Dict = dict(strategy.get("gates", {}))
    money_overlay: Dict[str, float] = dict(strategy.get("money_overlay", {}))

    raw = evaluate_factors(contexts)
    norm = normalize_frame(raw)
    cat_df = _category_scores(norm)
    contrib_weight = cat_df.attrs.get("contrib_weight", {})

    ctx_by_code = {c.ts_code: c for c in contexts}
    results: List[ScoredStock] = []

    for code in norm.index:
        ctx = ctx_by_code[code]
        cat_scores = {
            cat: float(cat_df.loc[code, f"cat_{cat}"])
            for cat in _CAT_CN
            if f"cat_{cat}" in cat_df.columns
        }

        # 组装门槛行：原始特征 + 结构类别分
        gate_row: Dict[str, float] = {k: ctx.get(k) for k in _GATE_FEAT_KEYS}
        gate_row["cat_structure"] = cat_scores.get("structure", np.nan)
        if "limit_up_pct" not in gate_cfg and "limit_up_pct" in strategy:
            gate_cfg["limit_up_pct"] = strategy["limit_up_pct"]
        passed, reasons = passes_gates(gate_row, gate_cfg)

        # 类别加权（money 除外，走 overlay）
        total = 0.0
        contrib: Dict[str, float] = {}
        for cat, cscore in cat_scores.items():
            if cat == "money":
                continue
            w_cat = float(weights.get(cat, 0.0))
            total += w_cat * cscore
            for fname, fshare in contrib_weight.get(cat, {}).items():
                contrib[fname] = w_cat * fshare * float(norm.loc[code, fname])

        # 资金 overlay
        overlay = 0.0
        if ctx.money_class and ctx.money_class in money_overlay:
            overlay = float(money_overlay[ctx.money_class])
        total += overlay

        results.append(
            ScoredStock(
                ts_code=code,
                name=ctx.name,
                industry=ctx.industry,
                total=float(total),
                passed=passed,
                gate_reasons=reasons,
                cat_scores=cat_scores,
                contrib=contrib,
                money_class=ctx.money_class,
                one_line=_one_line(cat_scores, weights, ctx.money_class, overlay),
                feat=dict(ctx.feat),
            )
        )

    results.sort(key=lambda s: s.total, reverse=True)
    return results


def _one_line(
    cat_scores: Dict[str, float],
    weights: Dict[str, float],
    money_class: Optional[str],
    overlay: float,
) -> str:
    """一句话归因：突出加权贡献最大的两个维度 + 资金判定。"""
    weighted = {
        cat: float(weights.get(cat, 0.0)) * s
        for cat, s in cat_scores.items()
        if cat != "money"
    }
    top = sorted(weighted.items(), key=lambda kv: kv[1], reverse=True)[:2]
    parts = [f"{_CAT_CN.get(c, c)}({v:.2f})" for c, v in top if v > 0]
    head = "＋".join(parts) if parts else "无突出维度"
    tail = ""
    if money_class:
        sign = "＋" if overlay >= 0 else "－"
        tail = f"，资金:{money_class}({sign}{abs(overlay):.2f})"
    return f"{head}领先{tail}"


def dedup_and_top(
    scored: List[ScoredStock],
    *,
    max_per_industry: int = 2,
    top_n: int = 6,
    require_pass: bool = True,
) -> List[ScoredStock]:
    """行业去重 + 取前 N（仅在通过门槛的股票中）。"""
    pool = [s for s in scored if s.passed] if require_pass else list(scored)
    final: List[ScoredStock] = []
    ind_count: Dict[str, int] = {}
    for s in pool:
        if ind_count.get(s.industry, 0) >= max_per_industry:
            continue
        final.append(s)
        ind_count[s.industry] = ind_count.get(s.industry, 0) + 1
        if len(final) >= top_n:
            break
    return final
