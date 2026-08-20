"""候选池构建测试。"""

from __future__ import annotations

import pandas as pd

from engine.universe import build_candidates


def test_candidate_seed_does_not_invent_missing_volume_ratio():
    frame = pd.DataFrame(
        [
            {
                "ts_code": "A.SZ",
                "industry": "行业",
                "pct_chg": 1.0,
                "amount": 100.0,
                "volume_ratio": 100.0,
            },
            {
                "ts_code": "B.SZ",
                "industry": "行业",
                "pct_chg": 1.0,
                "amount": 100.0,
                "volume_ratio": None,
            },
            {
                "ts_code": "C.SZ",
                "industry": "行业",
                "pct_chg": 1.0,
                "amount": 100.0,
                "volume_ratio": 1.0,
            },
        ]
    )

    candidates = build_candidates(
        frame,
        pd.DataFrame(),
        top_inds=["行业"],
        limit=3,
    )

    assert candidates["seed_score"].notna().all()
    assert candidates["seed_score"].nunique() == 1
