"""标签构建:T+N 前视收益。

**唯一允许的口径**(与 postmortem.HORIZONS 保持一致,不得另立):

    base   = close(as_of)
    target = close(第 N 个"市场交易日"之后)
    label  = target / base - 1

关键点:第 N 个交易日由 **trade_cal(市场日历)** 决定,不是"该股票下一根
可用K线"。区别在停牌股上是致命的——某票停牌 3 个月,它的"下一根K线"是
3 个月后,若用它当 T+1,标签就变成了 3 个月收益,IC 与胜率全部失真。
按市场日历取,则该票在目标日没有K线 → 标签缺失 → 样本丢弃,这是正确行为。

标签缺失原因必须分类上报,不能只给一个"缺失"计数:
- future_not_reached : 市场日历上还没走到第 N 个交易日(正常等待,会自愈)
- calendar_missing   : 日历本身没覆盖到那天(要回补 trade_cal,是人的活)
- target_bar_missing : 目标日该票无行情(停牌/退市)
- base_missing       : as_of 当日无基准收盘价
把"要等"和"要修"混成一个数字,就没人会去修。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import pandas as pd

# 与 postmortem.HORIZONS 同源:标签期限 -> 未来第 N 个交易日
HORIZONS: Dict[str, int] = {"ret1": 1, "ret3": 3, "ret5": 5, "ret10": 10}

MISSING_REASONS = (
    "future_not_reached",
    "calendar_missing",
    "target_bar_missing",
    "base_missing",
)


@dataclass
class LabelReport:
    """标签构建的产出统计。resolved 是可用样本数,missing 按原因分类。"""

    horizon: str
    n_days: int
    resolved: int = 0
    missing: Dict[str, int] = field(default_factory=dict)

    def note_missing(self, reason: str) -> None:
        if reason not in MISSING_REASONS:
            raise ValueError(f"未知标签缺失原因: {reason}")
        self.missing[reason] = self.missing.get(reason, 0) + 1

    def needs_attention(self) -> Dict[str, int]:
        """非"等未来"的缺失——这些要人处理,不该当成正常状态。"""
        return {k: v for k, v in self.missing.items() if k != "future_not_reached"}

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "n_days": self.n_days,
            "resolved": self.resolved,
            "missing": dict(self.missing),
            "needs_attention": self.needs_attention(),
        }


class TradingCalendar:
    """市场交易日历的只读视图。

    刻意做成显式对象而不是直接查库:标签构建是最容易出前视错误的地方,
    把"第 N 个交易日"的定义收敛到一处,便于单测覆盖(不需要数据库)。
    """

    def __init__(self, open_days: Iterable[str]) -> None:
        days = sorted({str(d) for d in open_days})
        self._days: List[str] = days
        self._index: Dict[str, int] = {d: i for i, d in enumerate(days)}

    def __len__(self) -> int:
        return len(self._days)

    @property
    def max_day(self) -> Optional[str]:
        return self._days[-1] if self._days else None

    @property
    def days(self) -> List[str]:
        return list(self._days)

    def contains(self, day: str) -> bool:
        return str(day) in self._index

    def sessions_after(self, day: str, n: int) -> Optional[str]:
        """day 之后第 n 个开市日;日历不够长则 None。

        day 本身不必在日历内(可能是非交易日):此时以"第一个 > day 的开市日"
        作为第 1 个。这样传入周末也能给出正确答案。
        """
        if n <= 0:
            raise ValueError("n 必须为正整数")
        day = str(day)
        pos = self._index.get(day)
        if pos is None:
            # day 不在日历内:二分找第一个严格大于 day 的开市日
            lo, hi = 0, len(self._days)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._days[mid] <= day:
                    lo = mid + 1
                else:
                    hi = mid
            target = lo + n - 1
        else:
            target = pos + n
        if target >= len(self._days):
            return None
        return self._days[target]


class CloseLookup:
    """(ts_code, trade_date) -> close 的快速查表。

    从长表一次性建索引,避免在标签循环里逐票查库(几万次单行查询)。
    """

    def __init__(self, daily: pd.DataFrame) -> None:
        if daily is None or daily.empty:
            self._map: Dict[tuple, float] = {}
            return
        frame = daily[["ts_code", "trade_date", "close"]].dropna(subset=["close"])
        self._map = {
            (str(code), str(date)): float(close)
            for code, date, close in zip(
                frame["ts_code"], frame["trade_date"], frame["close"], strict=False
            )
        }

    def get(self, ts_code: str, trade_date: str) -> Optional[float]:
        return self._map.get((str(ts_code), str(trade_date)))


def build_labels(
    samples: pd.DataFrame,
    *,
    calendar: TradingCalendar,
    closes: CloseLookup,
    horizon: str = "ret5",
) -> tuple[pd.Series, LabelReport]:
    """为样本表构建 T+N 收益标签。

    samples 需含 ts_code / as_of 两列。返回 (标签 Series, 统计报告);
    标签与 samples 索引对齐,无法计算的位置为 NaN——**不填 0**。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"未知期限: {horizon},可选 {sorted(HORIZONS)}")
    n = HORIZONS[horizon]
    report = LabelReport(horizon=horizon, n_days=n)

    if samples is None or samples.empty:
        return pd.Series(dtype="float64"), report

    for col in ("ts_code", "as_of"):
        if col not in samples.columns:
            raise ValueError(f"samples 缺少必需列: {col}")

    cal_max = calendar.max_day
    values: List[float] = []
    for ts_code, as_of in zip(samples["ts_code"], samples["as_of"], strict=False):
        as_of = str(as_of)
        base = closes.get(ts_code, as_of)
        if base is None or base <= 0:
            report.note_missing("base_missing")
            values.append(float("nan"))
            continue

        target = calendar.sessions_after(as_of, n)
        if target is None:
            # 日历里 as_of 之后不足 n 个开市日。两种情况必须分开:
            # 日历已延伸到 as_of 之后 -> 确实是未来没到;否则日历该回补了。
            if cal_max is not None and cal_max > as_of:
                report.note_missing("future_not_reached")
            else:
                report.note_missing("calendar_missing")
            values.append(float("nan"))
            continue

        future = closes.get(ts_code, target)
        if future is None:
            report.note_missing("target_bar_missing")
            values.append(float("nan"))
            continue

        values.append(float(future) / float(base) - 1.0)
        report.resolved += 1

    return pd.Series(values, index=samples.index, dtype="float64"), report


def to_binary(labels: pd.Series, threshold: float = 0.0) -> pd.Series:
    """连续收益 -> 二分类标签(1 = 收益 > threshold)。

    NaN 保持 NaN:不知道收益的样本既不是正例也不是负例,
    当成 0(负例)会凭空造出一批"亏损样本",把胜率算低。
    """
    out = pd.Series(float("nan"), index=labels.index, dtype="float64")
    mask = labels.notna()
    out[mask] = (labels[mask] > threshold).astype(float)
    return out
