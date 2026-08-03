"""离线重放今日打分,导出**全部**候选的真实结构化结果(含门槛失败原因)。

不联网、不写库。供 UI 展示真实的「结构化名单 / 门槛淘汰原因」。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from workbench.engine.config import load_settings, load_strategy, resolve_path  # noqa: E402
from workbench.engine.db import Store  # noqa: E402
from workbench.engine.run_scan import _build_contexts  # noqa: E402
from workbench.engine.score import dedup_and_top, score_pool  # noqa: E402
from workbench.engine.universe import (  # noqa: E402
    apply_universe,
    build_candidates,
    industry_heat,
    industry_meta,
)


def main(out: str) -> None:
    settings = load_settings()
    strat_name = settings["engine"]["default_strategy"]
    strat = load_strategy(strat_name)
    dbp = str(resolve_path(settings["data"]["db_path"]))
    bars = int(settings["engine"].get("history_bars", 150))
    cand_limit = int(settings["engine"].get("candidate_limit", 260))
    price_max = strat["universe"].get("price_max")

    with Store(dbp) as store:
        as_of = store.latest_date()
        snap = store.snapshot(as_of)
        m = apply_universe(snap, strat["universe"])
        ind = industry_heat(m)
        heat_map, rank_map, top_inds = industry_meta(ind)
        cand = build_candidates(m, ind, top_inds, cand_limit)
        contexts = _build_contexts(
            store, cand, heat_map, rank_map, top_inds, as_of, bars, price_max
        )
        scored = score_pool(contexts, strat)

    passed = [s for s in scored if s.passed]
    final = dedup_and_top(
        scored,
        max_per_industry=int(strat["dedup"]["max_per_industry"]),
        top_n=int(strat["top_n"]),
        require_pass=True,
    )
    final_codes = [s.ts_code for s in final]

    reason_counter: Counter[str] = Counter()
    for s in scored:
        for r in s.gate_reasons:
            reason_counter[r] += 1

    def row(s: Any) -> Dict[str, Any]:
        f = s.feat
        return {
            "ts_code": s.ts_code,
            "name": s.name,
            "industry": s.industry,
            "total": round(float(s.total), 6),
            "passed": bool(s.passed),
            "gate_reasons": list(s.gate_reasons),
            "money_class": s.money_class,
            "one_line": s.one_line,
            "selected": s.ts_code in final_codes,
            "contrib": {k: round(float(v), 6) for k, v in s.contrib.items()},
            "close": f.get("last_close"),
            "pct_chg": f.get("pct_chg"),
            "amount_yi": (float(f["amount"]) / 100000.0) if f.get("amount") else None,
            "ret20": f.get("ret20"),
            "pos60": f.get("pos60"),
            "macd_bull": f.get("macd_bull"),
            "weekly_bull": f.get("weekly_bull"),
            "vol_health": f.get("vol_health"),
            "ma_stack": f.get("ma_stack"),
            "net5": f.get("net5"),
            "big5": f.get("big5"),
            "industry_heat": f.get("industry_heat"),
            "industry_rank": f.get("industry_rank"),
        }

    ordered = sorted(scored, key=lambda s: float(s.total), reverse=True)
    doc = {
        "as_of": as_of,
        "strategy": strat_name,
        "candidate_count": int(len(cand)),
        "scored_count": int(len(scored)),
        "dropped_in_context": int(len(cand) - len(scored)),
        "passed_count": int(len(passed)),
        "final_count": int(len(final)),
        "final_codes": final_codes,
        "gate_reason_counts": dict(reason_counter.most_common()),
        "money_class_counts": dict(Counter(
            (s.money_class or "无资金流数据") for s in scored).most_common()),
        "industry_counts_passed": dict(Counter(
            s.industry for s in passed).most_common()),
        "rows": [row(s) for s in ordered],
    }
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    brief = {k: doc[k] for k in ("as_of", "candidate_count", "scored_count",
                                 "dropped_in_context", "passed_count", "final_count",
                                 "final_codes", "gate_reason_counts",
                                 "money_class_counts")}
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    print(f"\nwrote {out}  rows={len(doc['rows'])}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/scored_all.json")
