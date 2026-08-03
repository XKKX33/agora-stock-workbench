"""导出一次扫描的**真实**全链路事实,供 UI 使用。

只读 DuckDB + 重放 universe/热度 逻辑,不联网、不写库。
输出 JSON:每个流水线节点的真实进→出数字、行业热度榜、入选名单及其 contrib/feat。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from workbench.engine.config import load_settings, load_strategy, resolve_path  # noqa: E402
from workbench.engine.db import Store  # noqa: E402
from workbench.engine.universe import (  # noqa: E402
    build_candidates,
    industry_heat,
    industry_meta,
    is_mainboard,
)

NON_MAIN = ("300", "301", "688", "689", "8", "4", "9")


def _funnel(snap: pd.DataFrame, uni: Dict[str, Any]) -> Dict[str, Any]:
    """逐条硬过滤的真实淘汰量(顺序独立统计,便于展示各条口径)。"""
    total = int(len(snap))
    name = snap["name"].fillna("")
    close = snap["close"].astype(float)
    amount = snap["amount"].astype(float)
    price_max = float(uni["price_max"])
    min_amt = float(uni["min_amount_yi"]) * 100_000.0

    hit_st = int(name.str.contains(r"ST|\*", regex=True).sum())
    hit_board = int((~snap["symbol"].map(is_mainboard)).sum())
    hit_price = int((close >= price_max).sum())
    hit_amount = int((amount < min_amt).sum())

    keep = snap[
        (~name.str.contains(r"ST|\*", regex=True))
        & (close < price_max)
        & snap["symbol"].map(is_mainboard)
        & (amount >= min_amt)
    ]
    return {
        "cross_section": total,
        "excluded_st": hit_st,
        "excluded_non_mainboard": hit_board,
        "excluded_price_ge": {"threshold": price_max, "count": hit_price},
        "excluded_amount_lt": {"threshold_yi": uni["min_amount_yi"], "count": hit_amount},
        "after_universe": int(len(keep)),
    }


def main(out: str) -> None:
    settings = load_settings()
    strat = load_strategy(settings["engine"]["default_strategy"])
    uni = strat["universe"]
    dbp = str(resolve_path(settings["data"]["db_path"]))
    cand_limit = int(settings["engine"].get("candidate_limit", 260))

    with Store(dbp) as store:
        as_of = store.latest_date()
        snap = store.snapshot(as_of)
        funnel = _funnel(snap, uni)

        m = snap[
            (~snap["name"].fillna("").str.contains(r"ST|\*", regex=True))
            & (snap["close"].astype(float) < float(uni["price_max"]))
            & snap["symbol"].map(is_mainboard)
            & (snap["amount"].astype(float) >= float(uni["min_amount_yi"]) * 100_000.0)
        ].reset_index(drop=True)

        ind = industry_heat(m)
        heat_map, rank_map, top_inds = industry_meta(ind)
        cand = build_candidates(m, ind, top_inds, cand_limit)

        picks = store.con.execute(
            """
            select run_date, as_of, strategy, ts_code, name, industry, rank, total,
                   money_class, one_line, contrib_json, feat_json,
                   ret1, ret3, ret5, ret10
            from picks where as_of = ? order by rank
            """,
            [as_of],
        ).fetchdf()

    rows: List[Dict[str, Any]] = []
    for _, r in picks.iterrows():
        rows.append({
            "rank": int(r["rank"]),
            "ts_code": r["ts_code"],
            "name": r["name"],
            "industry": r["industry"],
            "total": float(r["total"]),
            "money_class": r["money_class"],
            "one_line": r["one_line"],
            "contrib": json.loads(r["contrib_json"]),
            "feat": json.loads(r["feat_json"]),
            "ret": {k: (None if pd.isna(r[k]) else float(r[k]))
                    for k in ("ret1", "ret3", "ret5", "ret10")},
        })

    doc = {
        "as_of": as_of,
        "strategy": settings["engine"]["default_strategy"],
        "strategy_label": strat.get("name"),
        "weights": strat["weights"],
        "gates": strat["gates"],
        "money_overlay": strat["money_overlay"],
        "top_n": strat["top_n"],
        "max_per_industry": strat["dedup"]["max_per_industry"],
        "funnel": funnel,
        "industry_total": int(len(ind)),
        "industry_top": ind.head(12).to_dict(orient="records"),
        "candidate_count": int(len(cand)),
        "picks": rows,
    }
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in
                      ("as_of", "strategy", "funnel", "industry_total", "candidate_count")},
                     ensure_ascii=False, indent=2))
    print(f"\nwrote {out}  picks={len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/scan_facts.json")
