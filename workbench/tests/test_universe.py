"""候选池构建测试。"""

from __future__ import annotations

import pandas as pd

from engine.universe import apply_universe, build_candidates, is_mainboard


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


def test_is_mainboard_reads_ts_code_not_symbol():
    """板块判据必须取 ts_code。symbol 来自 LEFT JOIN,可以是缺失值。"""
    assert is_mainboard("600000.SH") is True
    assert is_mainboard("002898.SZ") is True
    assert is_mainboard("300001.SZ") is False
    assert is_mainboard("301999.SZ") is False
    assert is_mainboard("688001.SH") is False
    # 北交所:920/430/830 前缀都不是主板
    assert is_mainboard("920305.BJ") is False
    assert is_mainboard("430047.BJ") is False
    assert is_mainboard("830799.BJ") is False


def _snap_row(ts_code, name, **over):
    row = {
        "ts_code": ts_code,
        "name": name,
        "symbol": None if name is None else ts_code.split(".")[0],
        "industry": "行业",
        "close": 10.0,
        "amount": 200000.0,
        "pct_chg": 1.0,
    }
    row.update(over)
    return row


def test_universe_excludes_rows_whose_filter_criteria_are_missing():
    """判据缺失一律排除。

    截面是 `daily LEFT JOIN stock_basic`,缺 stock_basic 行时 name/symbol 为空。
    旧实现把这种票当成"不含 ST"且"不是非主板前缀"从而放行,一只连板块都不
    知道的票会静默进候选池。
    """
    snap = pd.DataFrame(
        [
            _snap_row("600000.SH", "浦发银行"),
            _snap_row("600519.SH", "ST油服"),
            _snap_row("301999.SZ", "创业板票"),
            # 缺 stock_basic:name 与 symbol 都为空,但 ts_code 一定在
            _snap_row("920305.BJ", None),
            _snap_row("002898.SZ", None),
        ]
    )

    kept = apply_universe(
        snap,
        {"board": "mainboard", "exclude_st": True, "price_max": 70, "min_amount_yi": 0.8},
    )

    assert kept["ts_code"].tolist() == ["600000.SH"]


def test_universe_board_filter_survives_missing_symbol_column_values():
    """symbol 全为空也不影响板块过滤:判据来自 ts_code。"""
    snap = pd.DataFrame(
        [
            _snap_row("600000.SH", "浦发银行", symbol=None),
            _snap_row("300001.SZ", "创业板票", symbol=None),
        ]
    )

    kept = apply_universe(snap, {"board": "mainboard", "exclude_st": True})

    assert kept["ts_code"].tolist() == ["600000.SH"]
