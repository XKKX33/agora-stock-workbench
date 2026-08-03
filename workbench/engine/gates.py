"""硬门槛层（与打分解耦）。

门槛只做"是否入选"的布尔判定，不参与打分排序。
声明式读取策略 gates 配置，返回是否通过 + 未过原因，便于归因与调试。

门槛作用对象是一行已组装特征(dict)，同时包含：
- 原始特征(ret20/macd_bull/weekly_bull/vol_health/pct_chg ...)
- 归一化后的类别分(cat_structure ...)
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# 门槛键 -> (特征键, 比较方向)  ; 'ge' 表示 特征 >= 阈值
_NUMERIC_GATES = {
    "macd_bull_min": ("macd_bull", "ge"),
    "weekly_bull_min": ("weekly_bull", "ge"),
    "vol_score_min": ("vol_health", "ge"),
    "ret20_min": ("ret20", "ge"),
    "structure_pct_min": ("cat_structure", "ge"),
}


def passes_gates(row: Dict[str, float], gate_cfg: Dict) -> Tuple[bool, List[str]]:
    """返回 (是否通过, 未过门槛原因列表)。"""
    reasons: List[str] = []

    for gate_key, (feat_key, _op) in _NUMERIC_GATES.items():
        if gate_key not in gate_cfg:
            continue
        threshold = float(gate_cfg[gate_key])
        val = row.get(feat_key)
        if val is None or (isinstance(val, float) and val != val):  # NaN
            reasons.append(f"{feat_key}=NaN<{gate_key}")
            continue
        if float(val) < threshold:
            reasons.append(f"{feat_key}={float(val):.3g}<{threshold:g}")

    # 排除当日涨停/准涨停（主板 10cm，阈值取 settings.realtime.limit_up_pct）
    if gate_cfg.get("exclude_limit_up", False):
        limit_pct = float(gate_cfg.get("limit_up_pct", 9.5))
        pct = row.get("pct_chg")
        if pct is not None and float(pct) >= limit_pct:
            reasons.append(f"pct_chg={float(pct):.3g}>=涨停线{limit_pct:g}")

    return (len(reasons) == 0), reasons
