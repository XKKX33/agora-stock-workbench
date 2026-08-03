"""单只股票的因子上下文。

context 把"一只股票在某个交易日的所有派生特征"预计算好，
因子函数只从中读取标量、不重复计算，保证纯函数、可测试、无前视。

前视/泄漏纪律：
- 所有滚动、resample 只使用 <= 截面交易日(as_of)的历史。
- 快照行(snapshot)必须来自同一 as_of 交易日。
- moneyflow 属于事后确认字段，缺失时置 NaN，不得反填未来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _safe_float(x: Any, default: float = float("nan")) -> float:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return float(x)
    except Exception:
        return default


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.Series(close).astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


@dataclass
class StockContext:
    """一只候选股在 as_of 交易日的完整特征快照。"""

    ts_code: str
    name: str
    industry: str
    as_of: str
    # 预计算特征字典（因子从这里取值）
    feat: Dict[str, float] = field(default_factory=dict)
    # 原始快照行（pct_chg/amount/turnover_rate/volume_ratio 等）
    snapshot: Dict[str, Any] = field(default_factory=dict)
    # 资金流字段（事后确认，默认缺失）
    money: Dict[str, float] = field(default_factory=dict)
    money_class: Optional[str] = None

    def get(self, key: str, default: float = float("nan")) -> float:
        return _safe_float(self.feat.get(key, default), default)


def build_context(
    *,
    ts_code: str,
    name: str,
    industry: str,
    as_of: str,
    hist: pd.DataFrame,
    snapshot: Dict[str, Any],
    industry_heat: float = 0.0,
    industry_rank: int = 999,
    amount_top15pct: bool = False,
    min_bars: int = 70,
) -> Optional[StockContext]:
    """由日线历史 + 快照构建 StockContext。

    hist: 单只股票的日线，需含 open/high/low/close/vol/amount/trade_date，
          且已过滤到 <= as_of。返回 None 表示数据不足、应跳过。
    """
    if hist is None or len(hist) < min_bars:
        return None

    h = hist.sort_values("trade_date").reset_index(drop=True)
    close = h["close"].astype(float)
    high = h["high"].astype(float)
    low = h["low"].astype(float)
    vol = h["vol"].astype(float)
    amount = h["amount"].astype(float)

    dif, dea, hist_macd = macd(close)
    for n in (5, 10, 20, 60):
        h[f"ma{n}"] = close.rolling(n).mean()
    last = h.iloc[-1]
    last_close = float(last["close"])

    ret20 = last_close / float(close.iloc[-21]) - 1 if len(close) > 21 else float("nan")
    ret60 = last_close / float(close.iloc[-61]) - 1 if len(close) > 61 else float("nan")
    rng60 = max(float(high.tail(60).max() - low.tail(60).min()), 1e-9)
    pos60 = (last_close - float(low.tail(60).min())) / rng60
    new20 = bool(last_close >= float(high.iloc[-21:-1].max())) if len(high) > 21 else False
    new60 = bool(last_close >= float(high.iloc[-61:-1].max())) if len(high) > 61 else False

    vol5_20 = float(vol.tail(5).mean() / max(vol.tail(20).mean(), 1e-9))
    vol20_60 = float(vol.tail(20).mean() / max(vol.tail(60).mean(), 1e-9))
    amt5_20 = float(amount.tail(5).mean() / max(amount.tail(20).mean(), 1e-9))

    macd_bull = (
        int(dif.iloc[-1] > dea.iloc[-1])
        + int(dif.iloc[-1] > 0)
        + int(hist_macd.iloc[-1] > 0)
        + int(hist_macd.iloc[-1] >= hist_macd.iloc[-3])
    )

    # 周线
    hw = h.copy()
    hw["date"] = pd.to_datetime(hw["trade_date"])
    wk = (
        hw.set_index("date")
        .resample("W-FRI")
        .agg({"close": "last", "high": "max", "low": "min", "vol": "sum", "amount": "sum"})
        .dropna()
        .reset_index()
    )
    if len(wk) >= 30:
        wdif, wdea, whist = macd(wk["close"])
        weekly_bull = (
            int(wdif.iloc[-1] > wdea.iloc[-1])
            + int(whist.iloc[-1] > 0)
            + int(wk["close"].iloc[-1] > wk["close"].rolling(10).mean().iloc[-1])
            + int(wk["close"].iloc[-1] >= wk["high"].iloc[-13:-1].max())
        )
    else:
        weekly_bull = 0

    ma_stack = (
        int(last_close > last["ma5"])
        + int(last_close > last["ma10"])
        + int(last_close > last["ma20"])
        + int(last_close > last["ma60"])
    )

    # 结构：波浪打分的可分解成分（0-1 归一由 normalize 层做，这里给原始成分）
    breakout = (2 if new60 else (1 if new20 else 0))
    trend_combo = (
        2 if (ret20 > 0.12 and ret60 > 0.18)
        else (1 if (ret20 > 0.06 and ret60 > 0.1) else 0)
    )

    # 量能健康度（甜蜜区间打分，避免"越放量越好"的误导）
    vol_health = 0
    vol_health += 2 if 1.05 <= vol5_20 <= 3.5 else (1 if vol5_20 > 0.85 else 0)
    vol_health += 1 if vol20_60 > 1.05 else 0
    vol_health += 1 if amt5_20 > 1.05 else 0

    feat: Dict[str, float] = {
        "last_close": last_close,
        "pct_chg": _safe_float(snapshot.get("pct_chg"), 0.0),
        "ret20": ret20,
        "ret60": ret60,
        "pos60": pos60,
        "new20": float(new20),
        "new60": float(new60),
        "breakout": float(breakout),
        "trend_combo": float(trend_combo),
        "ma_stack": float(ma_stack),
        "macd_dif": float(dif.iloc[-1]),
        "macd_dea": float(dea.iloc[-1]),
        "macd_hist": float(hist_macd.iloc[-1]),
        "macd_bull": float(macd_bull),
        "weekly_bull": float(weekly_bull),
        "vol5_20": vol5_20,
        "vol20_60": vol20_60,
        "amt5_20": amt5_20,
        "vol_health": float(vol_health),
        "industry_heat": _safe_float(industry_heat, 0.0),
        "industry_rank": float(industry_rank),
        "amount_top15pct": 1.0 if amount_top15pct else 0.0,
        "amount": _safe_float(snapshot.get("amount"), 0.0),
    }

    return StockContext(
        ts_code=ts_code,
        name=name,
        industry=industry,
        as_of=as_of,
        feat=feat,
        snapshot=dict(snapshot),
    )
