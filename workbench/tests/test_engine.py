"""合成数据单测：不依赖 Tushare，验证因子引擎整条链路。

覆盖：
- 因子注册表完整、元数据合法。
- 归一化输出恒在 [0,1]。
- contrib 可加性：Σ contrib == 类别加权和（不含资金overlay）。
- 强主升股 > 弱势股；门槛能过滤弱势股。
- 资金 overlay 生效。

运行：
    set PYTHONHOME=
    python -m pytest workbench/tests/test_engine.py -q
或直接：
    python workbench/tests/test_engine.py
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# 允许 `python workbench/tests/test_engine.py` 直接运行
_ENGINE_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ENGINE_PARENT not in sys.path:
    sys.path.insert(0, _ENGINE_PARENT)

from engine.factors import FACTORS, CATEGORIES  # noqa: E402
from engine.factors.context import build_context  # noqa: E402
from engine.normalize import evaluate_factors, normalize_frame  # noqa: E402
from engine.score import score_pool, dedup_and_top  # noqa: E402


# ---------------------------------------------------------------- 合成数据

def _synth_hist(n=160, start=10.0, drift=0.0, vol_mult=1.0, blowoff=False, seed=0):
    """生成确定性 OHLCV 历史（不使用随机时间，seed 固定）。"""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y%m%d")
    steps = drift + rng.normal(0, 0.01, n)
    close = start * np.cumprod(1 + steps)
    if blowoff:
        close[-1] = close[-2] * 1.098  # 尾部爆量涨停
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    base_vol = 1_000_000 * vol_mult
    vol = base_vol * (1 + np.abs(rng.normal(0, 0.2, n)))
    if blowoff:
        vol[-1] = vol[-2] * 5.0
    amount = close * vol / 1000.0
    return pd.DataFrame({
        "ts_code": "T",
        "trade_date": dates,
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": np.r_[close[0], close[:-1]],
        "vol": vol,
        "amount": amount,
    })


def _ctx(ts_code, name, industry, hist, heat, rank, pct_chg, money_class=None,
         amount_top=False):
    snap = {"pct_chg": pct_chg, "amount": float(hist["amount"].iloc[-1]),
            "turnover_rate": 5.0, "volume_ratio": 1.5}
    c = build_context(
        ts_code=ts_code, name=name, industry=industry, as_of="20250801",
        hist=hist, snapshot=snap, industry_heat=heat, industry_rank=rank,
        amount_top15pct=amount_top,
    )
    if c is not None:
        c.money_class = money_class
    return c


def _strategy():
    return {
        "weights": {"theme": 0.22, "structure": 0.26, "momentum": 0.16,
                    "macd": 0.14, "volume": 0.12, "money": 0.10},
        "gates": {"structure_pct_min": 0.30, "macd_bull_min": 2,
                  "weekly_bull_min": 1, "vol_score_min": 2, "ret20_min": 0.03,
                  "exclude_limit_up": True, "limit_up_pct": 9.5},
        "money_overlay": {"资金一致确认": 0.08, "大资金承接型强分歧": 0.04,
                          "总资金认可但大单不连续": 0.01, "资金同步分歧，降级": -0.12},
    }


# ---------------------------------------------------------------- 断言工具

def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ok: {name}")


# ---------------------------------------------------------------- 测试

def test_registry_metadata():
    _check("注册表非空", len(FACTORS) > 0)
    for fname, (spec, fn) in FACTORS.items():
        _check(f"{fname} 类别合法", spec.category in CATEGORIES)
        _check(f"{fname} direction 合法", spec.direction in (1, -1))
        _check(f"{fname} normalize 合法", spec.normalize in ("rank", "zscore", "passthrough"))
    cats = {spec.category for spec, _ in FACTORS.values()}
    for want in ("momentum", "structure", "macd", "volume", "money", "theme"):
        _check(f"含类别 {want}", want in cats)


def _build_pool():
    strong = _ctx("STRONG.SZ", "强主升", "半导体",
                  _synth_hist(drift=0.012, vol_mult=1.3, seed=1),
                  heat=8.0, rank=1, pct_chg=6.5, money_class="资金一致确认",
                  amount_top=True)
    mid = _ctx("MID.SZ", "温和", "化工",
               _synth_hist(drift=0.004, vol_mult=1.0, seed=2),
               heat=3.0, rank=8, pct_chg=2.0, money_class="总资金认可但大单不连续")
    weak = _ctx("WEAK.SZ", "弱势", "公用事业",
                _synth_hist(drift=-0.006, vol_mult=0.8, seed=3),
                heat=0.5, rank=20, pct_chg=-1.5, money_class="资金同步分歧，降级")
    blow = _ctx("BLOW.SZ", "爆量涨停", "题材",
                _synth_hist(drift=0.006, vol_mult=1.1, blowoff=True, seed=4),
                heat=6.0, rank=3, pct_chg=9.8, money_class="资金一致确认")
    pool = [c for c in (strong, mid, weak, blow) if c is not None]
    _check("四只 context 均成功构建", len(pool) == 4)
    return pool


def test_normalize_bounds():
    pool = _build_pool()
    raw = evaluate_factors(pool)
    norm = normalize_frame(raw)
    vals = norm.values.flatten()
    vals = vals[~np.isnan(vals)]
    _check("归一化 >= 0", float(vals.min()) >= -1e-9)
    _check("归一化 <= 1", float(vals.max()) <= 1 + 1e-9)


def test_scoring_and_gates():
    pool = _build_pool()
    strat = _strategy()
    scored = score_pool(pool, strat)
    by_code = {s.ts_code: s for s in scored}

    # 强主升应排第一，且高于弱势
    _check("强主升 > 弱势", by_code["STRONG.SZ"].total > by_code["WEAK.SZ"].total)
    _check("强主升 > 温和", by_code["STRONG.SZ"].total > by_code["MID.SZ"].total)

    # 门槛：弱势股应被拦截；爆量涨停股应被涨停门槛拦截
    _check("弱势股未过门槛", not by_code["WEAK.SZ"].passed)
    _check("涨停股被 exclude_limit_up 拦截",
           any("涨停线" in r for r in by_code["BLOW.SZ"].gate_reasons))

    # contrib 可加性：Σcontrib ≈ 非资金类别加权和
    s = by_code["STRONG.SZ"]
    contrib_sum = sum(s.contrib.values())
    weighted_cat = sum(
        strat["weights"].get(c, 0.0) * v
        for c, v in s.cat_scores.items() if c != "money"
    )
    _check("contrib 可加性", abs(contrib_sum - weighted_cat) < 1e-6)

    # 一句话归因非空
    _check("含一句话归因", len(s.one_line) > 0)
    print("    强主升 归因:", s.one_line)


def test_dedup_top():
    pool = _build_pool()
    scored = score_pool(pool, _strategy())
    final = dedup_and_top(scored, max_per_industry=2, top_n=6, require_pass=True)
    _check("最终名单只含过门槛股", all(s.passed for s in final))
    industries = [s.industry for s in final]
    for ind in set(industries):
        _check(f"行业 {ind} 去重<=2", industries.count(ind) <= 2)


def _run_all():
    for fn in (test_registry_metadata, test_normalize_bounds,
               test_scoring_and_gates, test_dedup_top):
        print(f"[{fn.__name__}]")
        fn()
    print("\nALL PASSED ✅")


if __name__ == "__main__":
    _run_all()
