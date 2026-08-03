"""训练集构建:重放历史截面,产出 (特征, 标签) 表。

为什么必须重放,不能直接读 scan_rows
------------------------------------
scan_rows 只有实际扫描过的那几天(当前库里 2 次运行 / 520 行),
而机器学习需要**几十到上百个交易截面**才谈得上统计意义。
但 daily 表有完整历史(当前 171 个交易日)。所以正确做法是:
拿历史日线,按每个交易日重新跑一遍"候选池 → context → 因子 → 归一化",
得到与线上打分**同源同口径**的特征矩阵。

同源是关键:如果训练用一套特征、线上打分用另一套,模型评估出来的
IC 再高也说明不了线上那套有效。本模块刻意复用 universe / context /
normalize 三个线上模块,不自己实现任何特征计算。

前视纪律
--------
- 每个截面日 d 只用 <= d 的日线(store.history 已按 as_of 过滤)。
- 行业热度、amount 分位都只在 d 当天的截面内计算。
- 资金流(moneyflow)是事后确认字段,缺失置 NaN,不反填。
- 标签由 labels.py 用 trade_cal 定位 T+N,与特征严格分离。

成本提示:重放是 O(交易日 × 候选数) 次日线查询,比单次扫描慢很多。
故提供 stride(隔几天取一个截面)与 max_days 上限,默认只取近端若干天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..db import Store
from ..factors.context import build_context
from ..factors.money import classify_money
from ..normalize import evaluate_factors, normalize_frame
from ..universe import (
    amount_top15pct_threshold,
    apply_universe,
    build_candidates,
    industry_heat,
    industry_meta,
)
from .labels import CloseLookup, HORIZONS, LabelReport, TradingCalendar, build_labels

# 非特征列:落在样本表里但不能喂给模型(标识列 / 标签 / 未来信息)
META_COLUMNS = ("ts_code", "name", "industry", "as_of", "label")


@dataclass
class DatasetReport:
    """数据集构建过程的可解释统计。"""

    horizon: str
    requested_days: int = 0
    replayed_days: int = 0
    skipped_days: Dict[str, int] = field(default_factory=dict)
    n_rows: int = 0
    n_features: int = 0
    # 最后一个"标签已经能算出来"的交易日。晚于此日的截面不采样。
    label_cutoff: Optional[str] = None
    label_report: Optional[LabelReport] = None

    def note_skip(self, reason: str) -> None:
        self.skipped_days[reason] = self.skipped_days.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "requested_days": self.requested_days,
            "replayed_days": self.replayed_days,
            "skipped_days": dict(self.skipped_days),
            "n_rows": self.n_rows,
            "n_features": self.n_features,
            "label_cutoff": self.label_cutoff,
            "labels": self.label_report.as_dict() if self.label_report else None,
        }


def _money_for(store: Store, ts_code: str, as_of: str) -> tuple[Optional[str], Dict[str, float]]:
    """近 5 日资金分层。与 run_scan._money_class_for 同口径。"""
    mf = store.moneyflow_tail(ts_code, as_of, 5)
    if mf is None or mf.empty:
        return None, {}
    net5 = (
        float(mf["net_mf_amount"].astype(float).sum())
        if "net_mf_amount" in mf.columns else float("nan")
    )
    if {"buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"} <= set(mf.columns):
        big_daily = (
            (mf["buy_lg_amount"].astype(float) - mf["sell_lg_amount"].astype(float))
            + (mf["buy_elg_amount"].astype(float) - mf["sell_elg_amount"].astype(float))
        )
        big5 = float(big_daily.sum())
        big_pos_days = float((big_daily > 0).sum())
    else:
        big5 = float("nan")
        big_pos_days = float("nan")
    return classify_money(net5, big5), {
        "net5": net5, "big5": big5, "big_pos_days": big_pos_days,
    }


def replay_cross_section(
    store: Store,
    as_of: str,
    *,
    universe_cfg: Dict,
    candidate_limit: int,
    history_bars: int,
    with_money: bool = True,
) -> Optional[pd.DataFrame]:
    """重放单个交易日的截面,返回归一化后的因子矩阵(含标识列)。

    返回 None 表示该日无法构建(无截面数据 / 候选池为空)。
    复用线上模块,保证与 score.py 吃到的特征完全同源。
    """
    snap = store.snapshot(as_of)
    if snap is None or snap.empty:
        return None

    pool = apply_universe(snap, universe_cfg or {})
    if pool.empty:
        return None
    ind = industry_heat(pool)
    heat_map, rank_map, top_inds = industry_meta(ind)
    cand = build_candidates(pool, ind, top_inds, candidate_limit)
    if cand is None or cand.empty:
        return None

    amount_q85 = amount_top15pct_threshold(cand)
    top8 = set(top_inds[:8])

    contexts = []
    for _, row in cand.iterrows():
        ts_code = row["ts_code"]
        hist = store.history(ts_code, as_of, history_bars)
        industry = row.get("industry")
        ctx = build_context(
            ts_code=ts_code,
            name=row.get("name"),
            industry=industry,
            as_of=as_of,
            hist=hist,
            snapshot={
                "pct_chg": row.get("pct_chg"),
                "amount": row.get("amount"),
                "turnover_rate": row.get("turnover_rate"),
                "volume_ratio": row.get("volume_ratio"),
            },
            industry_heat=float(heat_map.get(industry, 0.0)),
            industry_rank=int(rank_map.get(industry, 999)),
            amount_top15pct=bool(float(row.get("amount", 0) or 0) > amount_q85),
        )
        if ctx is None:
            continue
        ctx.feat["industry_top8"] = 1.0 if industry in top8 else 0.0
        if with_money:
            money_class, money = _money_for(store, ts_code, as_of)
            ctx.money_class = money_class
            ctx.money = money
            for key, value in money.items():
                ctx.feat[key] = value
        contexts.append(ctx)

    if not contexts:
        return None

    raw = evaluate_factors(contexts)
    norm = normalize_frame(raw)
    if norm is None or norm.empty:
        return None

    frame = norm.copy()
    meta = {c.ts_code: c for c in contexts}
    frame.insert(0, "as_of", as_of)
    frame.insert(0, "industry", [meta[i].industry for i in frame.index])
    frame.insert(0, "name", [meta[i].name for i in frame.index])
    frame.insert(0, "ts_code", list(frame.index))
    return frame.reset_index(drop=True)


def build_dataset(
    store: Store,
    *,
    universe_cfg: Dict,
    candidate_limit: int = 260,
    history_bars: int = 150,
    horizon: str = "ret5",
    max_days: int = 60,
    stride: int = 1,
    exchange: str = "SSE",
    end: Optional[str] = None,
    with_money: bool = True,
) -> tuple[pd.DataFrame, DatasetReport]:
    """构建 (特征+标签) 训练表。

    max_days : 最多重放多少个交易截面(从 end 往前数)
    stride   : 隔几个交易日取一个截面。>1 可显著降低重放成本,
               同时降低相邻截面的高度重叠(相邻日特征几乎相同)。

    返回的表含 META_COLUMNS + 因子列;label 为 NaN 的行**保留**,
    由调用方决定丢弃 —— 丢弃动作与原因统计要分开,便于如实上报。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"未知期限: {horizon},可选 {sorted(HORIZONS)}")
    if stride < 1:
        raise ValueError("stride 必须 >= 1")
    report = DatasetReport(horizon=horizon)

    end_day = end or store.latest_date()
    if not end_day:
        return pd.DataFrame(), report

    # 采样上限必须扣掉最后 N 个开市日:那几天的 T+N 还没发生,
    # 标签必然是 NaN。硬采下来只会把"未来还没到"混进缺失统计里,
    # 看起来像数据有洞,其实是采样口径错了。
    n_days = HORIZONS[horizon]
    horizon_tail = store.open_dates(exchange, end_day, n_days + 1)
    label_cutoff = horizon_tail[0] if len(horizon_tail) == n_days + 1 else None
    if label_cutoff is None:
        report.note_skip("horizon_exceeds_history")
        return pd.DataFrame(), report
    report.label_cutoff = label_cutoff

    # 取截面候选日:市场日历上 <= label_cutoff 的开市日
    open_days = store.open_dates(exchange, label_cutoff, max_days * stride)
    if not open_days:
        return pd.DataFrame(), report
    picked = sorted(open_days)[::-1][::stride][:max_days]
    picked = sorted(picked)
    report.requested_days = len(picked)

    frames: List[pd.DataFrame] = []
    for day in picked:
        try:
            frame = replay_cross_section(
                store, day,
                universe_cfg=universe_cfg,
                candidate_limit=candidate_limit,
                history_bars=history_bars,
                with_money=with_money,
            )
        except Exception:
            # 单日重放失败不该让整个数据集构建失败,但要计数上报,
            # 否则"少了 30 天"会无声无息地变成"样本就这么多"。
            report.note_skip("replay_error")
            continue
        if frame is None or frame.empty:
            report.note_skip("no_cross_section")
            continue
        frames.append(frame)
        report.replayed_days += 1

    if not frames:
        return pd.DataFrame(), report

    samples = pd.concat(frames, ignore_index=True)

    # 标签:用完整日历 + 全量收盘价查表,避免逐行查库
    calendar = TradingCalendar(store.open_dates(exchange, store.calendar_max(exchange) or end_day, 10_000))
    closes = CloseLookup(_closes_for(store, samples))
    labels, label_report = build_labels(
        samples, calendar=calendar, closes=closes, horizon=horizon
    )
    samples["label"] = labels
    report.label_report = label_report
    report.n_rows = int(len(samples))
    report.n_features = len([c for c in samples.columns if c not in META_COLUMNS])
    return samples, report


def _closes_for(store: Store, samples: pd.DataFrame) -> pd.DataFrame:
    """取样本涉及股票的全部收盘价(建标签查表用)。

    一次性取回,避免在标签循环里做几万次单行查询。
    只读,不写、不建表。
    """
    codes = sorted({str(c) for c in samples["ts_code"]})
    if not codes:
        return pd.DataFrame(columns=["ts_code", "trade_date", "close"])
    placeholders = ",".join(["?"] * len(codes))
    return store.con.execute(
        f"""
        SELECT ts_code, trade_date, close FROM daily
        WHERE ts_code IN ({placeholders})
        """,
        codes,
    ).df()


def feature_columns(frame: pd.DataFrame) -> List[str]:
    """样本表里真正可作为模型输入的列。顺序固定(排序),保证产物可复现。"""
    return sorted([c for c in frame.columns if c not in META_COLUMNS])
