"""评估指标:算不出就报 None,绝不用 0 顶替。

核心原则(与全项目"缺失诚实"契约一致):
- 样本 < 3 的横截面 IC:算不出 -> None
- 全部同值(无方差)的相关系数:算不出 -> None
- 只有一个类别的 AUC:算不出 -> None(不是 0.5,那是"瞎猜"的意思,不一样)
- 没有亏损样本的盈亏比:算不出 -> None(不是 inf)

把"算不出"写成 0,页面上会显示"IC = 0 / 无区分度",这是**把没结论讲成了
坏结论**;写成 0.5 的 AUC 则是**把没结论讲成了瞎猜水平**。两者都是撒谎。

不依赖 scipy:Spearman 用"先 rank 再 Pearson"实现(与 postmortem.py 同法),
AUC 用秩和公式(Mann-Whitney U)实现,数值上与 sklearn.roc_auc_score 一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

MIN_CROSS_SECTION = 3  # 单日横截面最少样本数,低于此 IC 无意义


def _clean_pair(a: pd.Series, b: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"a": pd.to_numeric(a, errors="coerce"),
                         "b": pd.to_numeric(b, errors="coerce")}).dropna()


def correlation(a: pd.Series, b: pd.Series, *, method: str = "pearson") -> Optional[float]:
    """相关系数。样本不足或无方差返回 None。"""
    df = _clean_pair(a, b)
    if len(df) < MIN_CROSS_SECTION:
        return None
    if df["a"].nunique() < 2 or df["b"].nunique() < 2:
        return None
    x, y = (df["a"].rank(), df["b"].rank()) if method == "spearman" else (df["a"], df["b"])
    value = float(x.corr(y))
    return None if value != value else value


def cross_section_ic(
    frame: pd.DataFrame,
    *,
    score_col: str = "pred",
    label_col: str = "label",
    day_col: str = "as_of",
    method: str = "spearman",
) -> tuple[Optional[float], Optional[float], List[dict]]:
    """逐日横截面 IC,再对交易日取均值。

    返回 (IC均值, IC_IR, 每日明细)。逐日算再平均是必须的——把多日样本混在
    一起算一个大相关系数,会把"不同日期的整体涨跌差异"混进来,那不是选股能力。

    IC_IR = mean(IC) / std(IC),衡量 IC 的稳定性;只有一个有效交易日时为 None
    (一天的样本谈不上"稳定性")。
    """
    if frame is None or frame.empty:
        return None, None, []
    for col in (score_col, label_col, day_col):
        if col not in frame.columns:
            raise ValueError(f"缺少列: {col}")

    daily: List[dict] = []
    for day, group in frame.groupby(frame[day_col].astype(str), sort=True):
        value = correlation(group[score_col], group[label_col], method=method)
        daily.append({
            "as_of": day,
            "ic": round(value, 4) if value is not None else None,
            "n": int(len(group)),
        })

    valid = np.array([d["ic"] for d in daily if d["ic"] is not None], dtype="float64")
    if valid.size == 0:
        return None, None, daily
    ic_mean = float(valid.mean())
    ic_ir: Optional[float] = None
    if valid.size > 1:
        std = float(valid.std(ddof=0))
        if std > 1e-12:
            ic_ir = float(ic_mean / std)
    return ic_mean, ic_ir, daily


def auc(scores: pd.Series, binary_labels: pd.Series) -> Optional[float]:
    """ROC-AUC(Mann-Whitney U 秩和公式,正确处理并列值)。

    只有一个类别时返回 None:此时 AUC 在数学上无定义,
    报 0.5 会被误读成"模型等于随机",而事实是"没法评"。
    """
    df = _clean_pair(scores, binary_labels)
    if df.empty:
        return None
    y = df["b"]
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = df["a"].rank()  # 平均秩,自动处理并列
    rank_sum_pos = float(ranks[y == 1].sum())
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


def hit_rate(labels: pd.Series, *, threshold: float = 0.0) -> Optional[float]:
    """胜率:收益 > threshold 的占比。无有效样本返回 None。"""
    series = pd.to_numeric(labels, errors="coerce").dropna()
    if series.empty:
        return None
    return float((series > threshold).mean())


def profit_factor(labels: pd.Series) -> Optional[float]:
    """盈亏比 = 总盈利 / |总亏损|。无亏损样本时返回 None 而非 inf。"""
    series = pd.to_numeric(labels, errors="coerce").dropna()
    if series.empty:
        return None
    wins = float(series[series > 0].sum())
    losses = float(-series[series < 0].sum())
    if losses <= 1e-12:
        return None
    return wins / losses


def top_bucket_return(
    frame: pd.DataFrame,
    *,
    score_col: str = "pred",
    label_col: str = "label",
    day_col: str = "as_of",
    k: int = 5,
) -> Optional[float]:
    """每日取预测分最高的 k 只,看它们的平均实际收益。

    这是最贴近实盘的指标:实盘只买头部若干只,整体 IC 再高、
    头部不赚钱也没用。逐日取 top-k 再平均,不跨日混合。
    """
    if frame is None or frame.empty or k < 1:
        return None
    per_day: List[float] = []
    for _, group in frame.groupby(frame[day_col].astype(str), sort=True):
        sub = group.dropna(subset=[score_col, label_col])
        if sub.empty:
            continue
        top = sub.nlargest(min(k, len(sub)), score_col)
        per_day.append(float(pd.to_numeric(top[label_col]).mean()))
    if not per_day:
        return None
    return float(np.mean(per_day))


def decile_returns(
    frame: pd.DataFrame,
    *,
    score_col: str = "pred",
    label_col: str = "label",
    day_col: str = "as_of",
    n_buckets: int = 5,
) -> List[dict]:
    """分层收益:按预测分逐日分桶,看平均收益是否随分数单调递增。

    单调性比绝对数值更有说服力——它证明分数携带的是"程度"信息,
    而不是恰好某一档运气好。桶内样本不足的桶如实标注 n。
    """
    if frame is None or frame.empty or n_buckets < 2:
        return []
    buckets: Dict[int, List[float]] = {i: [] for i in range(n_buckets)}
    for _, group in frame.groupby(frame[day_col].astype(str), sort=True):
        sub = group.dropna(subset=[score_col, label_col])
        if len(sub) < n_buckets:
            continue  # 当日样本连分桶都不够,跳过而不是硬分
        ordered = sub.sort_values(score_col, ascending=False).reset_index(drop=True)
        edges = np.array_split(np.arange(len(ordered)), n_buckets)
        for bucket_index, positions in enumerate(edges):
            if len(positions) == 0:
                continue
            values = pd.to_numeric(ordered.loc[positions, label_col], errors="coerce").dropna()
            buckets[bucket_index].extend(values.tolist())
    out: List[dict] = []
    for index in range(n_buckets):
        values = buckets[index]
        out.append({
            "bucket": index + 1,          # 1 = 预测分最高档
            "n": len(values),
            "avg_return": round(float(np.mean(values)), 5) if values else None,
        })
    return out


def is_monotonic_decreasing(buckets: List[dict]) -> Optional[bool]:
    """分层收益是否随桶次单调递减(桶1最高)。有效桶 < 2 时返回 None。"""
    values = [b["avg_return"] for b in buckets if b.get("avg_return") is not None]
    if len(values) < 2:
        return None
    return all(values[i] >= values[i + 1] for i in range(len(values) - 1))


@dataclass
class EvalResult:
    """一折(或整体)的评估结果。所有字段可为 None,表示"算不出"。"""

    n_samples: int
    n_days: int
    ic_mean: Optional[float]
    ic_ir: Optional[float]
    pearson_ic: Optional[float]
    auc: Optional[float]
    hit_rate: Optional[float]
    profit_factor: Optional[float]
    top_bucket_return: Optional[float]
    buckets: List[dict]
    daily_ic: List[dict]

    def as_dict(self) -> dict:
        def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
            return round(value, digits) if value is not None else None

        return {
            "n_samples": self.n_samples,
            "n_days": self.n_days,
            "ic_mean": _round(self.ic_mean),
            "ic_ir": _round(self.ic_ir),
            "pearson_ic": _round(self.pearson_ic),
            "auc": _round(self.auc),
            "hit_rate": _round(self.hit_rate),
            "profit_factor": _round(self.profit_factor),
            "top_bucket_return": _round(self.top_bucket_return, 5),
            "buckets": self.buckets,
            "monotonic": is_monotonic_decreasing(self.buckets),
            "daily_ic": self.daily_ic,
        }


def evaluate_predictions(
    frame: pd.DataFrame,
    *,
    score_col: str = "pred",
    label_col: str = "label",
    day_col: str = "as_of",
    top_k: int = 5,
) -> EvalResult:
    """在"预测 + 实际标签"表上算全套指标。

    frame 需含 score_col / label_col / day_col。标签为 NaN 的样本自动排除
    ——它们是"未来还没到"或"停牌无行情",不是"收益为 0"。
    """
    clean = frame.dropna(subset=[score_col, label_col]) if frame is not None else pd.DataFrame()
    if clean.empty:
        return EvalResult(0, 0, None, None, None, None, None, None, None, [], [])

    ic_mean, ic_ir, daily = cross_section_ic(
        clean, score_col=score_col, label_col=label_col, day_col=day_col, method="spearman"
    )
    pearson, _, _ = cross_section_ic(
        clean, score_col=score_col, label_col=label_col, day_col=day_col, method="pearson"
    )
    binary = (pd.to_numeric(clean[label_col], errors="coerce") > 0).astype(float)
    return EvalResult(
        n_samples=int(len(clean)),
        n_days=int(clean[day_col].astype(str).nunique()),
        ic_mean=ic_mean,
        ic_ir=ic_ir,
        pearson_ic=pearson,
        auc=auc(clean[score_col], binary),
        hit_rate=hit_rate(clean[label_col]),
        profit_factor=profit_factor(clean[label_col]),
        top_bucket_return=top_bucket_return(
            clean, score_col=score_col, label_col=label_col, day_col=day_col, k=top_k
        ),
        buckets=decile_returns(
            clean, score_col=score_col, label_col=label_col, day_col=day_col
        ),
        daily_ic=daily,
    )
