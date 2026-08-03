"""自动复盘：回填历史选股的 T+N 收益，并做 IC / 胜率 / 分层自检。

系统"能不能自己评估选得准不准"的地基。分两层：

1. backfill_returns —— 为 picks 台账里 retN 仍为空的历史选股补算收益。
   口径（写死并强制遵守）：
     基准价  = as_of 当日收盘价
     retN    = close(as_of 之后第 N 个交易日) / close(as_of) - 1
   只有当"未来第 N 个交易日"已真实发生且本地已入库时才回填；
   否则保持 NULL —— 绝不用未来数据反填、绝不臆造。

2. evaluate —— 在已回填的样本上计算：
     - IC / RankIC(打分 total 与 retN 的相关性，衡量因子有没有区分度)
     - 胜率(retN > 0 占比)
     - 盈亏比(平均盈利 / 平均亏损绝对值)
     - 平均/中位收益、最大回撤(按选股序列近似)
     - 按 rank 分层的平均收益(检验"分高的是不是真的更强")

在线补数据（future_close 缺失时）交由 run_scan 的 ingest 负责，
本模块只做"本地已有数据的回填 + 统计"，保持纯粹、可离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .db import Store

# 回填的收益期限：列名 -> 未来第 N 个交易日
HORIZONS: Dict[str, int] = {"ret1": 1, "ret3": 3, "ret5": 5, "ret10": 10}


@dataclass
class BackfillReport:
    filled: Dict[str, int] = field(default_factory=dict)   # 每期限回填条数
    pending: Dict[str, int] = field(default_factory=dict)  # 每期限仍待回填
    # 待回填的具体原因分布,便于把"缺数据"和"未来未到"区分开:
    #   future_not_reached —— 目标交易日还没走到(超出已入库行情的最大日期,正常等待)
    #   calendar_missing   —— 日历本身没覆盖到那一天(需要回补 trade_cal)
    #   target_bar_missing —— 目标交易日全市场有行情、该票没有(停牌/退市)
    #   base_missing       —— as_of 当日无基准收盘价
    pending_reasons: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def total_filled(self) -> int:
        return sum(self.filled.values())

    def needs_attention(self) -> Dict[str, int]:
        """汇总"不是单纯等待未来"的待回填量——这些是要人处理的缺数据。"""
        out: Dict[str, int] = {}
        for reasons in self.pending_reasons.values():
            for name, cnt in reasons.items():
                if name != "future_not_reached":
                    out[name] = out.get(name, 0) + cnt
        return out


def backfill_returns(store: Store, exchange: str = "SSE") -> BackfillReport:
    """把 picks 台账里能算的 retN 都补上。返回回填统计。

    口径按市场交易日历(见 Store.future_close),停牌不顶替、不臆造。
    无法回填时记录**原因**而非只计一个 pending 数字:
    "日历没回补"和"未来还没到"在数字上都是 pending,但前者要人处理。

    区分"等未来"与"缺数据"的判据是**已入库行情的最大日期**(data_max),
    不是日历末日:日历是我们主动往前多拉的(见 run_scan._calendar_lookahead_end),
    它的末日在未来,说明不了任何事。目标日 > data_max 就是未来还没到;
    目标日 <= data_max 却查不到该票的收盘价,才是这只票真的缺行情。
    """
    report = BackfillReport(
        filled={c: 0 for c in HORIZONS},
        pending={c: 0 for c in HORIZONS},
        pending_reasons={c: {} for c in HORIZONS},
    )
    data_max = store.latest_date()

    def _note(col: str, reason: str) -> None:
        report.pending[col] += 1
        bucket = report.pending_reasons[col]
        bucket[reason] = bucket.get(reason, 0) + 1

    for col, n in HORIZONS.items():
        awaiting = store.open_picks_awaiting_return(col)
        if awaiting is None or awaiting.empty:
            continue
        for _, row in awaiting.iterrows():
            ts_code = row["ts_code"]
            as_of = row["as_of"]

            base = store.close_on(ts_code, as_of)
            if base is None or base <= 0:
                _note(col, "base_missing")
                continue

            target = store.sessions_after(exchange, as_of, n)
            if target is None:
                # 日历里 as_of 之后不足 n 个开市日。日历本该被拉到未来
                # (见 run_scan._calendar_lookahead_end),拉不出来就是日历该回补了。
                _note(col, "calendar_missing")
                continue

            if data_max is None or target > data_max:
                # 目标交易日还没走到(或本地一行行情都没有):正常等待,会自愈。
                _note(col, "future_not_reached")
                continue

            fut = store.close_on(ts_code, target)
            if fut is None:
                # 目标日已在已入库范围内,却查不到这只票 -> 停牌/退市,真的缺行情。
                _note(col, "target_bar_missing")
                continue

            store.update_pick_return(
                row["as_of"], row["strategy"], ts_code, col, float(fut / base - 1.0)
            )
            report.filled[col] += 1
    return report


# ------------------------------------------------------------ 统计指标

def _ic(scores: pd.Series, rets: pd.Series, method: str) -> float:
    """单日横截面相关系数。样本 < 3 或无方差时返回 NaN。

    Spearman(RankIC) 自行用"先 rank 再算 Pearson"实现,
    避免依赖 scipy(pandas 的 method='spearman' 底层需要 scipy)。
    """
    df = pd.DataFrame({"s": scores, "r": rets}).dropna()
    if len(df) < 3:
        return float("nan")
    if df["s"].nunique() < 2 or df["r"].nunique() < 2:
        return float("nan")
    if method == "spearman":
        a = df["s"].rank()
        b = df["r"].rank()
    else:
        a = df["s"]
        b = df["r"]
    return float(a.corr(b))  # 默认 Pearson


@dataclass
class HorizonStats:
    horizon: str
    n_samples: int
    ic_mean: float          # 各交易日横截面 Pearson IC 的均值
    rank_ic_mean: float     # 各交易日 RankIC(Spearman) 的均值
    ic_ir: float            # IC 均值 / IC 标准差(信息比率)
    win_rate: float
    profit_factor: float
    avg_ret: float
    median_ret: float
    layer_avg: Dict[str, float]  # 按 rank 分层的平均收益


def _profit_factor(rets: pd.Series) -> float:
    wins = rets[rets > 0].sum()
    losses = -rets[rets < 0].sum()
    if losses <= 1e-12:
        return float("inf") if wins > 0 else float("nan")
    return float(wins / losses)


def _layer_avg(df: pd.DataFrame, ret_col: str) -> Dict[str, float]:
    """按 rank 分层的平均收益：Top1 / Top2-3 / Top4+。"""
    out: Dict[str, float] = {}
    if "rank" not in df.columns:
        return out
    buckets = {
        "rank1": df[df["rank"] == 1],
        "rank2_3": df[df["rank"].between(2, 3)],
        "rank4plus": df[df["rank"] >= 4],
    }
    for name, sub in buckets.items():
        vals = sub[ret_col].dropna()
        if len(vals) > 0:
            out[name] = float(vals.mean())
    return out


def evaluate(store: Store, strategy: Optional[str] = None) -> List[HorizonStats]:
    """在已回填样本上计算各期限的 IC/胜率/盈亏比/分层收益。"""
    picks = store.all_picks(strategy)
    if picks is None or picks.empty:
        return []

    stats: List[HorizonStats] = []
    for col in HORIZONS:
        sample = picks.dropna(subset=[col])
        if sample.empty:
            continue
        rets = sample[col].astype(float)

        # 逐交易日算横截面 IC，再对交易日取均值（避免跨日混合失真）
        ics: List[float] = []
        rank_ics: List[float] = []
        for _, day in sample.groupby("as_of"):
            ics.append(_ic(day["total"], day[col], "pearson"))
            rank_ics.append(_ic(day["total"], day[col], "spearman"))
        ic_arr = np.array([x for x in ics if x == x])       # 去 NaN
        rank_arr = np.array([x for x in rank_ics if x == x])
        ic_mean = float(ic_arr.mean()) if ic_arr.size else float("nan")
        rank_ic_mean = float(rank_arr.mean()) if rank_arr.size else float("nan")
        ic_ir = (
            float(ic_arr.mean() / ic_arr.std())
            if ic_arr.size > 1 and ic_arr.std() > 1e-12
            else float("nan")
        )

        stats.append(HorizonStats(
            horizon=col,
            n_samples=int(len(sample)),
            ic_mean=ic_mean,
            rank_ic_mean=rank_ic_mean,
            ic_ir=ic_ir,
            win_rate=float((rets > 0).mean()),
            profit_factor=_profit_factor(rets),
            avg_ret=float(rets.mean()),
            median_ret=float(rets.median()),
            layer_avg=_layer_avg(sample, col),
        ))
    return stats


def stats_as_dict(s: HorizonStats) -> dict:
    """把一个期限的统计转成可 JSON 序列化的字典。

    NaN 与 inf 一律转 None:样本不足算不出 IC、没有亏损样本算不出盈亏比,
    这些是"算不出",不是 0,也不是"无穷好"。前端据 None 显示"样本不足"。
    """
    return {
        "horizon": s.horizon,
        "n_samples": s.n_samples,
        "ic_mean": round(s.ic_mean, 4) if s.ic_mean == s.ic_mean else None,
        "rank_ic_mean": round(s.rank_ic_mean, 4) if s.rank_ic_mean == s.rank_ic_mean else None,
        "ic_ir": round(s.ic_ir, 4) if s.ic_ir == s.ic_ir else None,
        "win_rate": round(s.win_rate, 4),
        "profit_factor": (
            round(s.profit_factor, 4)
            if s.profit_factor == s.profit_factor and s.profit_factor != float("inf")
            else None
        ),
        "avg_ret": round(s.avg_ret, 4),
        "median_ret": round(s.median_ret, 4),
        "layer_avg": {k: round(v, 4) for k, v in s.layer_avg.items()},
    }


def run_postmortem(store: Store, strategy: Optional[str] = None) -> dict:
    """一键复盘：先回填，再统计。返回可直接 JSON 序列化的摘要。"""
    bf = backfill_returns(store)
    stats = evaluate(store, strategy)
    return {
        "backfill": {
            "filled": bf.filled,
            "pending": bf.pending,
            "pending_reasons": bf.pending_reasons,
            # 非"等未来"的待回填量:>0 说明有数据要回补,不该当成正常状态
            "needs_attention": bf.needs_attention(),
            "total_filled": bf.total_filled(),
        },
        "stats": [stats_as_dict(s) for s in stats],
    }


def _cli() -> None:
    import argparse
    import json

    from .config import load_settings, resolve_path

    ap = argparse.ArgumentParser(description="自动复盘：回填 T+N 收益并做 IC 自检")
    ap.add_argument("--strategy", default=None, help="仅统计指定策略;默认全部")
    ap.add_argument("--db", default=None, help="DuckDB 路径;默认取 settings")
    args = ap.parse_args()

    settings = load_settings()
    dbp = args.db or str(resolve_path(settings["data"]["db_path"]))
    with Store(dbp) as store:
        summary = run_postmortem(store, args.strategy)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
