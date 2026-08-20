"""回测层:把 picks 台账上已回填的 retN 汇总成组合层面的净值、回撤、换手。

为什么不能把每天的 retN 直接连乘
--------------------------------
`picks` 每个交易日都记一批选股,retN 是"从 as_of 起 N 个交易日的收益"。
若每天开一笔并把 ret5 连乘,同一份资金会被重复计算 5 次——净值曲线凭空放大,
放大倍数刚好是持仓期长度。这类口径错误算出来的年化能到三位数,
看着像策略很强,其实是把重叠持仓当成了独立收益。

所以本模块只做一种口径:**不重叠调仓**(``non_overlap``)。
从最早的截面起,每隔 N 个可用截面开一笔,上一笔结清后才开下一笔。
每期收益都是真能拿到的。代价是只用掉 1/N 的截面,这个覆盖率在
``coverage`` 里如实给出,不藏。

为什么不提供"每天各投 1/N"的分批口径
------------------------------------
那种口径要按日给组合估值,而台账只存 T+N 的一个点,没有中间的逐日净值。
硬要画成日线就得在两点之间线性插值——插出来的回撤是画的,不是量的。
宁可少一种口径,也不给一条编出来的曲线。

缺失一律不当 0
--------------
某一期只要有一只票的 retN 还没回填(T+N 未到 / 日历缺失 / 目标 K 线缺失),
整期跳过并记原因,不拿剩下的票凑一个"看起来完整"的收益。
具体缺在哪一步由 `engine/postmortem.BackfillReport.pending_reasons` 回答,
本模块只负责不把它当成 0。算不出的指标返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

# 各期限的持仓交易日数,与 engine/postmortem.HORIZONS 同源
HORIZON_DAYS: Dict[str, int] = {"ret1": 1, "ret3": 3, "ret5": 5, "ret10": 10}


def horizons() -> List[str]:
    """按持仓天数排序的期限列表。

    不能用 sorted(HORIZON_DAYS):那是按字符串排,ret10 会插到 ret1 和 ret3 之间。
    下拉框顺序错乱本身不致命,但它出现在接口负载里,前端照着渲染就错了。
    """
    return sorted(HORIZON_DAYS, key=lambda name: HORIZON_DAYS[name])


# A 股双边成本粗估:佣金 2.5bp×2 + 印花税 5bp(仅卖出) + 滑点约 20bp。
# 这是**假设**不是事实,所以结果里毛/净两条都给,并把本值原样带出去——
# 一条净值曲线不说明成本口径,等于没法复核。
DEFAULT_COST_BPS = 30.0

TRADING_DAYS_PER_YEAR = 244

# 年化至少要有这么长的样本跨度;更短的样本上 CAGR 只是噪声放大器
MIN_DAYS_FOR_CAGR = 30

# 夏普至少要这么多期,否则标准差本身就不可信
MIN_PERIODS_FOR_SHARPE = 4


def _turnover(prev_codes: Optional[Sequence[str]], codes: Sequence[str]) -> float:
    """等权组合从 prev_codes 调到 codes 的换手率(占组合市值的比例)。

    口径是**权重变化的一半**:``sum|w_new - w_old| / 2``,等权下 ``w = 1/n``。
    除以 2 是因为一次调仓里卖出额和买入额相等(卖旧的钱买新的),
    只算其中一边才是"这次动了多大比例的仓",cost_bps 按双边成本计价。

    为什么不能用 ``1 - kept / len(codes)``:那个分母是**新**篮子。
    篮子变大时它恰好等于权重口径,变小时却会错——5 只缩到 3 只且 3 只全留仓,
    它算出 0,等于把卖掉的两只(占 40% 仓位)白送,不收任何成本。
    权重口径对两个方向都成立:留仓的票也要从 20% 补到 33%,那也是真实交易。

    首期建仓没有上一期持仓,记满仓 1.0。按权重公式只会算出 0.5(只有买没有卖),
    但建仓是一次实打实的全额买入,记 1.0 偏保守——宁可高估成本,
    不让净值曲线因为口径而好看。
    """
    if not prev_codes:
        return 1.0
    old_weight = 1.0 / len(prev_codes)
    new_weight = 1.0 / len(codes)
    old_set, new_set = set(prev_codes), set(codes)
    drift = 0.0
    for code in old_set | new_set:
        held_old = old_weight if code in old_set else 0.0
        held_new = new_weight if code in new_set else 0.0
        drift += abs(held_new - held_old)
    return drift / 2.0


def _finite(value) -> Optional[float]:
    """NaN / inf 一律转 None。"算不出"和"算出来是 0"必须分开。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _parse_day(day: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(day), "%Y%m%d")
    except (TypeError, ValueError):
        return None


def max_drawdown(
    equity: Sequence[float],
) -> Tuple[Optional[float], Optional[int], Optional[int]]:
    """返回 (最大回撤, 峰值下标, 谷值下标)。回撤取正数,0.12 即 -12%。

    没有任何有效净值时回撤是 None(算不出),一路上涨时是 0.0(算出来没回撤)。
    """
    peak: Optional[float] = None
    peak_index = 0
    worst = 0.0
    pair: Tuple[Optional[int], Optional[int]] = (None, None)
    for index, raw in enumerate(equity):
        value = _finite(raw)
        if value is None or value <= 0:
            continue
        if peak is None or value > peak:
            peak, peak_index = value, index
            continue
        drop = 1.0 - value / peak
        if drop > worst:
            worst = drop
            pair = (peak_index, index)
    if peak is None:
        return None, None, None
    return worst, pair[0], pair[1]


def _profit_factor(returns: Sequence[float]) -> Optional[float]:
    """盈亏比。没有亏损期时是 inf,inf 不是一个可读数字,按"算不出"处理。"""
    wins = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    if losses <= 1e-12:
        return None
    return wins / losses


@dataclass(frozen=True)
class Period:
    """一次调仓的结果。gross 是毛收益,net 扣掉买卖成本。"""

    as_of: str
    codes: Tuple[str, ...]
    gross_return: float
    net_return: float
    turnover: float
    gross_equity: float
    equity: float
    buy_turnover: float = 0.0
    sell_turnover: float = 0.0

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "n_holdings": len(self.codes),
            "codes": list(self.codes),
            "gross_return": round(self.gross_return, 6),
            "net_return": round(self.net_return, 6),
            "turnover": round(self.turnover, 4),
            "buy_turnover": round(self.buy_turnover, 4),
            "sell_turnover": round(self.sell_turnover, 4),
            "gross_equity": round(self.gross_equity, 6),
            "equity": round(self.equity, 6),
        }


@dataclass(frozen=True)
class SkippedPeriod:
    """被跳过的调仓期。reason 说清是缺数据、不可成交还是当天无选股。"""

    as_of: str
    reason: str
    n_missing: int = 0
    def as_dict(self) -> dict:
        return {"as_of": self.as_of, "reason": self.reason, "n_missing": self.n_missing}

@dataclass
class BacktestResult:
    strategy: str
    horizon: str
    top_k: int
    cost_bps: float
    strategy_config_hash: Optional[str] = None
    signal_start: Optional[str] = None
    signal_end: Optional[str] = None
    visible_cutoff: Optional[str] = None
    buy_cost_bps: float = 0.0
    sell_cost_bps: float = 0.0
    rebalance_mode: str = "non_overlap"
    limit_up_fill_policy: str = "skip"
    legacy_cost_bps: Optional[float] = None
    cost_conversion: Optional[str] = None
    periods: List[Period] = field(default_factory=list)
    skipped: List[SkippedPeriod] = field(default_factory=list)
    available_days: int = 0
    scheduled_periods: int = 0
    missing_reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.periods)

    @property
    def planned_periods(self) -> int:
        return self.scheduled_periods

    @property
    def measurable_sample(self) -> int:
        return len(self.periods)

    @property
    def coverage_ratio(self) -> Optional[float]:
        return (len(self.periods) / self.scheduled_periods) if self.scheduled_periods else None

    @property
    def equity_curve(self) -> List[float]:
        if not self.periods:
            return []
        return [1.0] + [p.equity for p in self.periods]

    @property
    def gross_curve(self) -> List[float]:
        if not self.periods:
            return []
        return [1.0] + [p.gross_equity for p in self.periods]

    @property
    def has_interior_gap(self) -> bool:
        if not self.periods or not self.skipped:
            return False
        last = self.periods[-1].as_of
        return any(item.as_of < last for item in self.skipped)

    def _span_days(self) -> Optional[int]:
        if not self.periods:
            return None
        first = _parse_day(self.periods[0].as_of)
        last = _parse_day(self.periods[-1].as_of)
        if first is None or last is None:
            return None
        tail = round(HORIZON_DAYS[self.horizon] * 7 / 5)
        return (last - first).days + tail

    def _cagr(self, span_days: Optional[int]) -> Optional[float]:
        if not self.periods or span_days is None or span_days < MIN_DAYS_FOR_CAGR:
            return None
        final = self.equity_curve[-1]
        if final <= 0:
            return None
        return final ** (365.25 / span_days) - 1.0

    def _sharpe(self, returns: List[float]) -> Optional[float]:
        if len(returns) < MIN_PERIODS_FOR_SHARPE:
            return None
        series = pd.Series(returns, dtype="float64")
        std = float(series.std(ddof=1))
        if std <= 1e-12:
            return None
        per_year = TRADING_DAYS_PER_YEAR / HORIZON_DAYS[self.horizon]
        return float(series.mean()) / std * (per_year ** 0.5)

    def metrics(self) -> Dict[str, Optional[float]]:
        if not self.periods:
            return {}
        net = [p.net_return for p in self.periods]
        turnovers = [p.turnover for p in self.periods]
        span_days = self._span_days()
        drawdown, _, _ = max_drawdown(self.equity_curve)
        return {
            "n_periods": len(self.periods),
            "span_days": span_days,
            "total_return": _finite(self.equity_curve[-1] - 1.0),
            "gross_total_return": _finite(self.gross_curve[-1] - 1.0),
            "cagr": _finite(self._cagr(span_days)),
            "max_drawdown": drawdown,
            "win_rate": sum(1 for r in net if r > 0) / len(net),
            "avg_period_return": sum(net) / len(net),
            "best_period": max(net),
            "worst_period": min(net),
            "avg_turnover": sum(turnovers) / len(turnovers),
            "sharpe": _finite(self._sharpe(net)),
            "profit_factor": _finite(_profit_factor(net)),
        }

    def as_dict(self) -> dict:
        labels = (["起点"] + [p.as_of for p in self.periods]) if self.periods else []
        drawdown, peak, trough = max_drawdown(self.equity_curve)
        curve = [
            {"label": labels[i], "equity": round(value, 6), "gross_equity": round(self.gross_curve[i], 6)}
            for i, value in enumerate(self.equity_curve)
        ]
        assumptions = {
            "mode": self.rebalance_mode,
            "rebalance_mode": self.rebalance_mode,
            "mode_note": f"每隔 {HORIZON_DAYS[self.horizon]} 个截面开一笔,上一笔结清后再开下一笔,不做重叠持仓",
            "cost_bps": self.cost_bps,
            "buy_cost_bps": self.buy_cost_bps,
            "sell_cost_bps": self.sell_cost_bps,
            "limit_up_fill_policy": self.limit_up_fill_policy,
            "cost_note": "成本按买入和卖出端分别计价,换手率为 sum(abs(w_new-w_old))/2",
            "weighting": "等权买入 rank 前 N 名",
            "sharpe_note": f"夏普按无风险利率 = 0 算,假设各期独立同分布,调仓期数少于 {MIN_PERIODS_FOR_SHARPE} 期直接不给值",
        }
        if self.legacy_cost_bps is not None:
            assumptions["legacy_cost_bps"] = self.legacy_cost_bps
            assumptions["cost_conversion"] = self.cost_conversion or "equal_buy_sell"
        return {
            "strategy": self.strategy,
            "strategy_config_hash": self.strategy_config_hash,
            "horizon": self.horizon,
            "top_k": self.top_k,
            "signal_date_range": {"start": self.signal_start, "end": self.signal_end},
            "visible_cutoff": self.visible_cutoff,
            "available": self.available,
            "missing_reason": self.missing_reason,
            "planned_periods": self.scheduled_periods,
            "measured_periods": len(self.periods),
            "skipped_periods": len(self.skipped),
            "interior_gap": self.has_interior_gap,
            "measurable_sample": self.measurable_sample,
            "coverage_ratio": self.coverage_ratio,
            "assumptions": assumptions,
            "coverage": {
                "available_days": self.available_days,
                "scheduled_periods": self.scheduled_periods,
                "measured_periods": len(self.periods),
                "skipped_periods": len(self.skipped),
                "has_interior_gap": self.has_interior_gap,
            },
            "metrics": self.metrics(),
            "drawdown": {
                "max": drawdown,
                "peak_label": labels[peak] if peak is not None and labels else None,
                "trough_label": labels[trough] if trough is not None and labels else None,
            },
            "equity_curve": curve,
            "periods": [p.as_dict() for p in self.periods],
            "skipped": [s.as_dict() for s in self.skipped],
        }

def _trade_turnover(prev_codes: Optional[Sequence[str]], codes: Sequence[str]) -> Tuple[float, float, float]:
    """Return (total, buy-side, sell-side) turnover from equal-weight deltas."""
    if prev_codes is None:
        return 1.0, 1.0, 0.0
    previous = {str(code): 1.0 / len(prev_codes) for code in prev_codes}
    current = {str(code): 1.0 / len(codes) for code in codes}
    buy = sum(max(current.get(code, 0.0) - previous.get(code, 0.0), 0.0) for code in set(previous) | set(current))
    sell = sum(max(previous.get(code, 0.0) - current.get(code, 0.0), 0.0) for code in set(previous) | set(current))
    return (buy + sell) / 2.0, buy, sell

def run_backtest(
    picks: Optional[pd.DataFrame],
    *,
    horizon: str = "ret5",
    strategy: Optional[str] = None,
    top_k: int = 5,
    cost_bps: Optional[float] = DEFAULT_COST_BPS,
    buy_cost_bps: Optional[float] = None,
    sell_cost_bps: Optional[float] = None,
    strategy_config_hash: Optional[str] = None,
    signal_start: Optional[str] = None,
    signal_end: Optional[str] = None,
    visible_cutoff: Optional[str] = None,
    rebalance_mode: str = "non_overlap",
    limit_up_fill_policy: str = "skip",
) -> BacktestResult:
    """在 picks 台账上跑固定输入、非重叠调仓回测。"""
    if horizon not in HORIZON_DAYS:
        raise ValueError(f"未知期限: {horizon}(可选 {sorted(HORIZON_DAYS)})")
    if rebalance_mode != "non_overlap":
        raise ValueError("仅支持 non_overlap 调仓模式")
    legacy_effective = False
    if cost_bps is not None and (buy_cost_bps is None and sell_cost_bps is None):
        legacy = float(cost_bps)
        buy_cost_bps = sell_cost_bps = legacy / 2.0
        legacy_effective = True
        legacy_cost = legacy
        conversion = "equal_buy_sell"
    else:
        legacy_cost = None
        conversion = None
        buy_cost_bps = 0.0 if buy_cost_bps is None else float(buy_cost_bps)
        sell_cost_bps = 0.0 if sell_cost_bps is None else float(sell_cost_bps)
        cost_bps = float(buy_cost_bps + sell_cost_bps)
    result = BacktestResult(
        strategy=strategy or "全部策略",
        horizon=horizon,
        top_k=int(top_k),
        cost_bps=float(cost_bps or 0.0),
        strategy_config_hash=strategy_config_hash,
        signal_start=signal_start,
        signal_end=signal_end,
        visible_cutoff=visible_cutoff,
        buy_cost_bps=float(buy_cost_bps),
        sell_cost_bps=float(sell_cost_bps),
        rebalance_mode=rebalance_mode,
        limit_up_fill_policy=limit_up_fill_policy,
        legacy_cost_bps=legacy_cost,
        cost_conversion=conversion,
    )
    frame = picks if picks is not None else pd.DataFrame()
    if not frame.empty and strategy and "strategy" in frame.columns:
        frame = frame[frame["strategy"].astype(str) == str(strategy)]
    if frame.empty:
        result.missing_reason = "no_picks"
        return result
    for column in ("as_of", "ts_code", horizon):
        if column not in frame.columns:
            result.missing_reason = f"column_missing:{column}"
            return result
    as_of_key = frame["as_of"].astype(str)
    if signal_start is not None:
        frame = frame[as_of_key >= str(signal_start)]
    if signal_end is not None:
        frame = frame[as_of_key <= str(signal_end)]
    if visible_cutoff is not None:
        frame = frame[frame["as_of"].astype(str) <= str(visible_cutoff)]
    days = sorted({str(d) for d in frame["as_of"].dropna()})
    result.available_days = len(days)
    if not days:
        result.missing_reason = "no_picks"
        return result
    step = HORIZON_DAYS[horizon]
    result.scheduled_periods = (len(days) + step - 1) // step
    prev_codes: Optional[Tuple[str, ...]] = None
    equity = 1.0
    gross_equity = 1.0
    index = 0
    while index < len(days):
        day = days[index]
        index += step
        basket = frame[frame["as_of"].astype(str) == day]
        if "rank" in basket.columns:
            basket = basket.sort_values("rank")
        basket = basket.head(int(top_k))
        if basket.empty:
            result.skipped.append(SkippedPeriod(day, "no_picks_on_day"))
            continue
        if limit_up_fill_policy == "skip" and "entry_status" in basket.columns:
            status = basket["entry_status"].astype(str).str.lower()
            unavailable = status.isin({"entry_unavailable", "limit_up_locked", "entry_bar_missing", "pending_entry", "future_not_visible", "future_not_reached"})
            if unavailable.any():
                result.skipped.append(SkippedPeriod(day, "entry_unavailable", int(unavailable.sum())))
                continue
        returns = pd.to_numeric(basket[horizon], errors="coerce")
        n_missing = int(returns.isna().sum())
        if n_missing:
            result.skipped.append(SkippedPeriod(day, "return_not_backfilled", n_missing))
            continue
        codes = tuple(str(c) for c in basket["ts_code"])
        gross = float(returns.mean())
        turnover, buy_turnover, sell_turnover = _trade_turnover(prev_codes, codes)
        if legacy_effective:
            net = gross - float(cost_bps) / 10000.0 * turnover
        else:
            net = gross - (float(buy_cost_bps) / 10000.0 * buy_turnover) - (float(sell_cost_bps) / 10000.0 * sell_turnover)
        net = round(net, 12)
        gross_equity *= 1.0 + gross
        equity *= 1.0 + net
        result.periods.append(Period(day, codes, gross, net, turnover, gross_equity, equity, buy_turnover, sell_turnover))
        prev_codes = codes
    if not result.periods:
        result.missing_reason = "no_measurable_period"
    return result


def compare_strategies(
    picks: Optional[pd.DataFrame],
    *,
    horizon: str = "ret5",
    top_k: int = 5,
    cost_bps: Optional[float] = DEFAULT_COST_BPS,
    buy_cost_bps: Optional[float] = None,
    sell_cost_bps: Optional[float] = None,
) -> List[BacktestResult]:
    """逐策略跑一遍,供并排对比。"""
    if picks is None or picks.empty or "strategy" not in picks.columns:
        return []
    names = sorted({str(s) for s in picks["strategy"].dropna()})
    return [run_backtest(picks, horizon=horizon, strategy=name, top_k=top_k,
                         cost_bps=cost_bps, buy_cost_bps=buy_cost_bps,
                         sell_cost_bps=sell_cost_bps) for name in names]
def from_store(
    store,
    *,
    horizon: str = "ret5",
    strategy: Optional[str] = None,
    top_k: int = 5,
    cost_bps: float = DEFAULT_COST_BPS,
) -> BacktestResult:
    """读路径入口。store 必须以 ensure_schema=False 打开:回测不建表、不写库。"""
    return run_backtest(
        store.all_picks(strategy),
        horizon=horizon,
        strategy=strategy,
        top_k=top_k,
        cost_bps=cost_bps,
    )







