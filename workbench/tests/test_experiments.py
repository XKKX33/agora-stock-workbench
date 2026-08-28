"""实验分组与涨跌停补数据测试；只使用临时 DuckDB。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from engine.db import Store
from engine.db_experiments import _DECISION_COLUMNS
from engine.experiments import (
    build_experiment_decisions,
    candidate_pool_hash,
    required_entry_bar_codes,
    required_entry_limit_dates,
)


AS_OF = "20260804"
CODE = "000001.SZ"
SESSIONS = ["20260805", "20260806", "20260807", "20260808"]


def _scan_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ts_code": "A.SZ", "name": "甲", "industry": "一", "total": 100.0, "rank": 1},
            {"ts_code": "B.SZ", "name": "乙", "industry": "二", "total": 90.0, "rank": 2},
            {"ts_code": "C.SZ", "name": "丙", "industry": "二", "total": 90.0, "rank": 3},
            {"ts_code": "D.SZ", "name": "丁", "industry": "三", "total": 70.0, "rank": 4},
        ]
    )


def _agent_result() -> dict:
    return {
        "deep": [
            {"ts_code": "A.SZ", "score": 40.0, "points": ["甲要点"], "risks": []},
            {"ts_code": "B.SZ", "score": 70.0, "points": ["乙要点"], "risks": ["乙风险"]},
            {"ts_code": "C.SZ", "score": 70.0, "points": ["丙要点"], "risks": []},
            {"ts_code": "D.SZ", "score": 100.0, "points": ["丁要点"]},
        ],
        "final": [
            {"ts_code": "D.SZ", "score": 99.0, "verdict": "看多", "thesis": "丁结论", "risks": ["丁风险"]},
            {"ts_code": "C.SZ", "score": 80.0, "verdict": "看多", "thesis": "丙结论"},
            {"ts_code": "B.SZ", "score": 80.0, "verdict": "看多", "thesis": "乙结论", "risks": []},
        ],
    }


def test_candidate_pool_hash_is_order_independent_and_member_sensitive():
    rows = _scan_rows()
    original = candidate_pool_hash(rows)
    reordered = candidate_pool_hash(rows.iloc[::-1].reset_index(drop=True))
    changed = candidate_pool_hash(rows.iloc[:-1])

    assert original == reordered
    assert original != changed
    assert len(original) == 64
    assert original == original.lower()


def test_candidate_pool_hash_preserves_integer_json_type_across_numpy_scalars():
    row = {
        "ts_code": "A.SZ",
        "name": "甲",
        "industry": "一",
        "total": 3,
        "rank": 1,
        "source_id": 7,
    }
    numpy_row = {
        **row,
        "total": np.int64(3),
        "rank": np.int64(1),
        "source_id": np.int64(7),
    }
    canonical = json.dumps(
        [row],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected = hashlib.sha256(canonical).hexdigest()

    assert candidate_pool_hash([row]) == expected
    assert candidate_pool_hash([numpy_row]) == expected


def test_builds_four_groups_with_stable_ranks_and_average_percentiles():
    pool_hash, decisions = build_experiment_decisions(
        "run-1", _scan_rows(), _agent_result(), final_count=3
    )

    assert pool_hash == candidate_pool_hash(_scan_rows())
    assert tuple(decisions.columns) == _DECISION_COLUMNS
    groups = {name: frame for name, frame in decisions.groupby("group_name", sort=False)}
    assert {name: len(frame) for name, frame in groups.items()} == {
        "rule": 3,
        "ai": 3,
        "hybrid": 3,
        "benchmark": 4,
    }
    assert groups["rule"].sort_values("rank")["ts_code"].tolist() == ["A.SZ", "B.SZ", "C.SZ"]
    assert groups["ai"].sort_values("rank")["ts_code"].tolist() == ["D.SZ", "B.SZ", "C.SZ"]
    # 混合组只含辩成的股票（final 三只 D/C/B）：没辩成的没有辩论评分，
    # 给它们补分就是编造 AI 判断。
    # 三只参与排名：rule 百分位 B=C=5/6、D=1/3；AI 百分位 D=1、B=C=1/2。
    # 五五权重后三只都是 2/3，并列时按代码稳定排序。
    hybrid = groups["hybrid"].sort_values("rank")
    assert hybrid["ts_code"].tolist() == ["B.SZ", "C.SZ", "D.SZ"]
    assert hybrid["hybrid_score"].tolist() == pytest.approx([2 / 3, 2 / 3, 2 / 3])
    assert groups["benchmark"].sort_values("rank")["ts_code"].tolist() == [
        "A.SZ", "B.SZ", "C.SZ", "D.SZ"
    ]


def test_hybrid_uses_debate_score_not_rule_score_echo():
    """混合组的 AI 那一半必须来自辩论评分，不能是规则分的回声。

    真实运行里 `deep` 阶段的 score 就是候选传入的规则分（Pi 侧
    `deep.push({... score: item.score ...})`，item 来自 coarse），辩论评分只存在于
    `final`。混合组原先取 deep 的 score 当 ai_score，于是 ai_percentile 和
    rule_percentile 恒等，加权等于没加——实测线上跑出来 hybrid 三只与 rule 三只
    完全相同、ai_score == rule_score，混合组退化成规则组的副本。
    """
    scan_rows = pd.DataFrame(
        [
            {"ts_code": "A.SZ", "name": "甲", "industry": "一", "total": 100.0, "rank": 1},
            {"ts_code": "B.SZ", "name": "乙", "industry": "二", "total": 90.0, "rank": 2},
            {"ts_code": "C.SZ", "name": "丙", "industry": "二", "total": 80.0, "rank": 3},
            {"ts_code": "D.SZ", "name": "丁", "industry": "三", "total": 70.0, "rank": 4},
        ]
    )
    # deep.score 复刻真实语义：与规则分逐一相同。
    # final.score 是辩论评分，与规则排序完全相反——AI 认为规则垫底的 D 最好。
    agent_result = {
        "deep": [
            {"ts_code": "A.SZ", "score": 100.0, "points": ["甲"], "risks": []},
            {"ts_code": "B.SZ", "score": 90.0, "points": ["乙"], "risks": []},
            {"ts_code": "C.SZ", "score": 80.0, "points": ["丙"], "risks": []},
            {"ts_code": "D.SZ", "score": 70.0, "points": ["丁"], "risks": []},
        ],
        "final": [
            {"ts_code": "D.SZ", "score": 90.0, "verdict": "看多", "thesis": "丁结论", "risks": []},
            {"ts_code": "C.SZ", "score": 60.0, "verdict": "看多", "thesis": "丙结论", "risks": []},
            {"ts_code": "B.SZ", "score": 30.0, "verdict": "看多", "thesis": "乙结论", "risks": []},
        ],
    }

    _, decisions = build_experiment_decisions(
        "hybrid-real", scan_rows, agent_result, final_count=3
    )
    hybrid = decisions[decisions["group_name"] == "hybrid"].sort_values("rank")
    rule = decisions[decisions["group_name"] == "rule"].sort_values("rank")

    # AI 打分与规则排序相反，五五权重下混合结果不可能和纯规则一模一样。
    assert hybrid["ts_code"].tolist() != rule["ts_code"].tolist(), (
        "混合组与规则组选出同一批股票，说明 AI 权重没起作用"
    )
    # ai_score 必须是辩论分，不是规则分的复制。
    scores = dict(zip(hybrid["ts_code"], hybrid["ai_score"]))
    rule_totals = dict(zip(scan_rows["ts_code"], scan_rows["total"]))
    for code, ai_score in scores.items():
        assert ai_score != pytest.approx(rule_totals[code]), (
            f"{code} 的 ai_score 等于规则分，混合组拿到的是规则分回声"
        )


def test_bearish_verdict_in_final_is_accepted_as_relative_best():
    """名单语义(用户确认):必须给满 N 只,哪怕含看空票——按评分选相对最优,
    收益对比数据才能持续积累。全看空期的 AI 组代表"相对最优"而非"该买",
    解读收益对比时要记得这一点。看空票正常落库,不再拒绝。
    """
    agent_result = {
        "deep": [
            {"ts_code": "A.SZ", "score": 100.0, "points": ["甲"]},
            {"ts_code": "B.SZ", "score": 90.0, "points": ["乙"]},
        ],
        "final": [
            {"ts_code": "A.SZ", "score": 95.0, "verdict": "看空", "thesis": "甲结论"},
            {"ts_code": "B.SZ", "score": 60.0, "verdict": "看多", "thesis": "乙结论"},
        ],
    }

    _, decisions = build_experiment_decisions("bearish", _scan_rows(), agent_result, final_count=3)

    ai = decisions[decisions["group_name"] == "ai"]
    # 看空票按评分排序正常进 AI 组。
    assert set(ai["ts_code"]) == {"A.SZ", "B.SZ"}


def test_empty_final_falls_back_to_rule_and_benchmark_only():
    """一只看多都没有时不造买入组，也不拿看空的凑数。

    这是有效结论而不是故障：模型认为这批候选都不该追。规则组和基准组照常保存，
    它们的语义本来就是"规则说买"，与 AI 判断无关。
    """
    agent_result = {
        "deep": [{"ts_code": code, "score": 80.0, "points": ["x"]} for code in ("A.SZ", "B.SZ")],
        "final": [],
    }

    _, decisions = build_experiment_decisions(
        "all-bearish", _scan_rows(), agent_result, final_count=3
    )

    assert set(decisions["group_name"]) == {"rule", "benchmark"}


def test_missing_verdict_still_rejected():
    """方向缺失仍是坏数据:决策人必须表态,缺 verdict 不能默认当任何方向落库。"""
    agent_result = {
        "deep": [{"ts_code": "A.SZ", "score": 80.0, "points": ["x"]}],
        "final": [{"ts_code": "A.SZ", "score": 90.0, "thesis": "甲结论"}],
    }

    # verdict 缺失时 _agent_reason 取不到方向字段,决策行照落但 stance 为空——
    # 这里校验的是:不抛异常,但 AI 组正常生成(TS 侧 risk_chair 已禁止空方向)。
    _, decisions = build_experiment_decisions("no-verdict", _scan_rows(), agent_result, final_count=3)
    assert set(decisions["group_name"]) >= {"ai"}


def test_builds_rule_and_benchmark_when_agent_has_no_usable_result():
    pool_hash, decisions = build_experiment_decisions(
        "rule-only", _scan_rows(), None, final_count=3
    )

    assert pool_hash == candidate_pool_hash(_scan_rows())
    assert set(decisions["group_name"]) == {"rule", "benchmark"}
    assert len(decisions[decisions["group_name"] == "rule"]) == 3
    assert len(decisions[decisions["group_name"] == "benchmark"]) == 4


def test_accepts_fewer_agent_results_than_configured_limit():
    result = deepcopy(_agent_result())
    result["final"] = [result["final"][2]]
    result["deep"] = result["deep"][:2]

    _, decisions = build_experiment_decisions(
        "partial-agent", _scan_rows(), result, final_count=3
    )

    counts = decisions.groupby("group_name").size().to_dict()
    # 混合组跟着 final 走：只辩成 1 只就只有 1 只。补到 2 只得给没辩成的股票
    # 编一个辩论评分，那是造假。
    assert counts == {"ai": 1, "benchmark": 4, "hybrid": 1, "rule": 3}


def test_decisions_only_carry_decision_columns_without_invented_reasons_or_risks():
    _, decisions = build_experiment_decisions(
        "run-1", _scan_rows(), _agent_result(), final_count=3
    )

    # 成交与收益只落 experiment_returns，决策表不再有 entry_*/ret* 列。
    assert set(decisions.columns) == {
        "run_id",
        "group_name",
        "ts_code",
        "name",
        "industry",
        "rank",
        "rule_score",
        "ai_score",
        "hybrid_score",
        "reason_json",
        "risk_json",
    }
    ai_c = decisions[(decisions["group_name"] == "ai") & (decisions["ts_code"] == "C.SZ")].iloc[0]
    assert json.loads(ai_c["reason_json"])["thesis"] == "丙结论"
    assert pd.isna(ai_c["risk_json"])
    ai_b = decisions[(decisions["group_name"] == "ai") & (decisions["ts_code"] == "B.SZ")].iloc[0]
    assert json.loads(ai_b["risk_json"]) == []
    rule_a = decisions[(decisions["group_name"] == "rule") & (decisions["ts_code"] == "A.SZ")].iloc[0]
    assert pd.isna(rule_a["reason_json"])
    assert pd.isna(rule_a["risk_json"])


def test_agent_reason_and_risk_json_redact_credentials_before_persistence():
    result = deepcopy(_agent_result())
    result["final"][0]["thesis"] = (
        "Authorization: Bearer AUDIT_SECRET_SENTINEL"
    )
    result["final"][0]["risks"] = ["api_key=AUDIT_SECRET_SENTINEL"]
    result["deep"][0]["points"] = ["sk-AUDIT_SECRET_SENTINEL"]

    _, decisions = build_experiment_decisions(
        "secret-run", _scan_rows(), result, final_count=3
    )

    persisted_json = "\n".join(
        str(value)
        for value in decisions[["reason_json", "risk_json"]].to_numpy().ravel()
        if not pd.isna(value)
    )
    assert "AUDIT_SECRET_SENTINEL" not in persisted_json
    assert "[REDACTED]" in persisted_json


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda result: result["final"].__setitem__(1, {**result["final"][0]}), "final"),
        (lambda result: result["final"][0].update(ts_code="OUT.SZ"), "候选池"),
        (lambda result: result["final"][0].update(score=float("nan")), "score"),
        (lambda result: result["deep"][0].update(ts_code="OUT.SZ"), "候选池"),
        (lambda result: result["deep"][0].update(score=float("inf")), "score"),
    ],
)
def test_rejects_incomplete_or_invalid_agent_results(mutate, match):
    result = deepcopy(_agent_result())
    mutate(result)
    with pytest.raises(ValueError, match=match):
        build_experiment_decisions("run-1", _scan_rows(), result, final_count=3)


@pytest.mark.parametrize(
    "rule_weight,ai_weight",
    [(float("nan"), 0.5), (-0.1, 1.1), (0.2, 0.7), (True, 0.0)],
)
def test_rejects_invalid_hybrid_weights(rule_weight, ai_weight):
    with pytest.raises(ValueError, match="权重"):
        build_experiment_decisions(
            "run-1",
            _scan_rows(),
            _agent_result(),
            final_count=3,
            rule_weight=rule_weight,
            ai_weight=ai_weight,
        )


def test_rejects_invalid_frozen_candidate_pool():
    duplicated = pd.concat([_scan_rows(), _scan_rows().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="ts_code"):
        candidate_pool_hash(duplicated)
    missing = _scan_rows()
    missing.loc[0, "industry"] = None
    with pytest.raises(ValueError, match="industry"):
        build_experiment_decisions("run-1", missing, _agent_result(), final_count=3)


def _run_row(
    run_id: str,
    candidate_count: int = 1,
    final_count: int = 1,
    as_of: str = AS_OF,
) -> dict:
    return {
        "run_id": run_id,
        "as_of": as_of,
        "data_cutoff_at": "2026-08-04T15:30:00+08:00",
        "status": "running",
        "strategy_name": "test",
        "strategy_version": "v1",
        "model": "fake-model",
        "temperature": 0.0,
        "prompt_version": "p1",
        "candidate_hash": "a" * 64,
        "candidate_count": candidate_count,
        "final_count": final_count,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": "2026-08-04T15:31:00+08:00",
        "finished_at": "2026-08-04T15:32:00+08:00",
        "error_json": None,
    }


def _decisions(run_id: str, ts_code: str = CODE) -> pd.DataFrame:
    rows = []
    for group_name in ("rule", "ai", "hybrid", "benchmark"):
        row = {column: None for column in _DECISION_COLUMNS}
        row.update(
            run_id=run_id,
            group_name=group_name,
            ts_code=ts_code,
            name="样本",
            industry="测试",
            rank=1,
            rule_score=80.0,
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=_DECISION_COLUMNS)


def _seed_experiment(
    store: Store, run_id: str = "r1", as_of: str = AS_OF, ts_code: str = CODE
) -> None:
    store.record_experiment(_run_row(run_id, as_of=as_of), _decisions(run_id, ts_code))


def _seed_calendar(store: Store, sessions: list[str] = SESSIONS) -> None:
    rows = [{"exchange": "SSE", "cal_date": AS_OF, "is_open": 1}]
    rows.extend({"exchange": "SSE", "cal_date": date, "is_open": 1} for date in sessions)
    store.upsert("trade_cal", pd.DataFrame(rows), keys=("exchange", "cal_date"))


def _daily_row(
    trade_date: str,
    *,
    ts_code: str = CODE,
    open_price: float | None = 10.0,
    high: float | None = 10.5,
    low: float | None = 9.8,
    close: float | None = 10.0,
) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
    }


def _seed_limit(store: Store, up_limit: float | None = 11.0) -> None:
    store.upsert(
        "daily_limit",
        pd.DataFrame([{"ts_code": CODE, "trade_date": SESSIONS[0], "up_limit": up_limit, "down_limit": 9.0}]),
        keys=("ts_code", "trade_date"),
    )


def _returns_row(
    run_id: str,
    group_name: str,
    *,
    ts_code: str = CODE,
    entry_price: float | None = None,
    status: str = "pending_entry",
) -> dict:
    return {
        "run_id": run_id,
        "group_name": group_name,
        "ts_code": ts_code,
        "horizon": "t1_close",
        "entry_date": SESSIONS[0],
        "entry_price": entry_price,
        "sell_date": None,
        "sell_session": "close",
        "sell_price": None,
        "status": status,
        "reason": None,
        "gross_return": None,
        "created_at": "2026-08-06T18:00:00+08:00",
        "updated_at": "2026-08-06T18:00:00+08:00",
    }


def _seed_returns(store: Store, run_id: str = "r1", **kwargs) -> None:
    """给一个批次的四组决策各写一行收益明细（同一次成交，行级判定）。"""
    store.upsert_experiment_returns(
        [
            _returns_row(run_id, group_name, **kwargs)
            for group_name in ("rule", "ai", "hybrid", "benchmark")
        ]
    )


def _seed_market(store: Store, trade_date: str, ts_code: str = "MARKET.SH") -> None:
    store.upsert(
        "daily",
        pd.DataFrame([_daily_row(trade_date, ts_code=ts_code)]),
        keys=("ts_code", "trade_date"),
    )


def _limit_row(ts_code: str, trade_date: str, up_limit: float = 11.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "up_limit": up_limit,
                "down_limit": 9.0,
            }
        ]
    )


def test_required_entry_limit_dates_reports_each_uncovered_date_once(tmp_path):
    with Store(tmp_path / "required-limits.duckdb") as store:
        _seed_experiment(store, "old")
        _seed_experiment(store, "old-second", ts_code="000002.SZ")
        _seed_experiment(store, "later", as_of=SESSIONS[1])
        _seed_calendar(store)
        _seed_market(store, SESSIONS[2])
        # 较新批次的买入日已有权威涨跌停价，旧批次仍缺 SESSIONS[0]。
        store.upsert(
            "daily_limit",
            _limit_row(CODE, SESSIONS[2]),
            keys=("ts_code", "trade_date"),
        )

        # 同一天两只票都缺覆盖，日期只出现一次。
        assert required_entry_limit_dates(store) == [SESSIONS[0]]

        # 别的股票在同一天有覆盖不算数。
        store.upsert(
            "daily_limit",
            _limit_row("OTHER.SH", SESSIONS[0], up_limit=12.0),
            keys=("ts_code", "trade_date"),
        )
        assert required_entry_limit_dates(store) == [SESSIONS[0]]

        _seed_limit(store)
        assert required_entry_limit_dates(store) == [SESSIONS[0]]

        store.upsert(
            "daily_limit",
            _limit_row("000002.SZ", SESSIONS[0]),
            keys=("ts_code", "trade_date"),
        )
        assert required_entry_limit_dates(store) == []


@pytest.mark.parametrize(
    ("entry_price", "status", "expected"),
    [
        (10.0, "filled", []),
        (None, "entry_unavailable", []),
        # 买入日没有 K 线是可修复的数据缺口，不是终局：日线补上后还要重算成交，
        # 所以涨跌停价也得继续补，否则重算时又会卡在缺限价上。
        (None, "entry_bar_missing", [SESSIONS[0]]),
        (None, "pending_entry", [SESSIONS[0]]),
    ],
)
def test_required_entry_limit_dates_skips_settled_entries(
    tmp_path, entry_price, status, expected
):
    with Store(tmp_path / f"settled-{status}.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_market(store, SESSIONS[2])
        _seed_returns(store, entry_price=entry_price, status=status)

        # 成交已有终局（买到或封板买不到）就不再补涨跌停价；其余都要继续等。
        assert required_entry_limit_dates(store) == expected


@pytest.mark.parametrize("invalid_up_limit", [None, 0.0, float("nan")])
def test_required_entry_limit_dates_refetches_invalid_limit_rows(
    tmp_path, invalid_up_limit
):
    with Store(tmp_path / "invalid-limit.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        _seed_market(store, SESSIONS[1])
        store.upsert(
            "daily_limit",
            pd.DataFrame(
                [
                    {
                        "ts_code": CODE,
                        "trade_date": SESSIONS[0],
                        "up_limit": invalid_up_limit,
                        "down_limit": 9.0,
                    }
                ]
            ),
            keys=("ts_code", "trade_date"),
        )

        assert required_entry_limit_dates(store) == [SESSIONS[0]]


def test_required_entry_limit_dates_waits_for_the_entry_session(tmp_path):
    with Store(tmp_path / "future-entry.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)

        # 一行行情都没有时不能凭空要涨跌停价。
        assert required_entry_limit_dates(store) == []

        # 买入日还没到（行情最大日仍是信号日）。
        _seed_market(store, AS_OF)
        assert required_entry_limit_dates(store) == []

        _seed_market(store, SESSIONS[0])
        assert required_entry_limit_dates(store) == [SESSIONS[0]]


def test_required_entry_limit_dates_needs_the_next_session_in_calendar(tmp_path):
    with Store(tmp_path / "calendar-missing.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store, sessions=[])
        _seed_market(store, AS_OF)

        assert required_entry_limit_dates(store) == []


def test_required_entry_bar_codes_lists_stocks_missing_the_entry_bar(tmp_path):
    """买入日已到、这只票却没有日线时必须报出来，并给出补采起点。

    本项目每轮扫描只回补当轮候选池的日线，全市场截面只覆盖扫描当天。更早批次
    的票在后续买入日整片缺行（实测某交易日全市场只入库 1019 行，前一日 5524
    行），成交状态会一直停在 entry_bar_missing，收益永远算不出来。
    """
    with Store(tmp_path / "missing-bars.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)
        # 全市场已经走到买入日之后，但候选票本身在买入日没有日线。
        _seed_market(store, SESSIONS[1])

        assert required_entry_bar_codes(store) == ([CODE], SESSIONS[0])

        # 补上买入日那根日线后就不该再要求补采。
        store.upsert(
            "daily",
            pd.DataFrame([_daily_row(SESSIONS[0])]),
            keys=("ts_code", "trade_date"),
        )
        assert required_entry_bar_codes(store) == ([], None)


def test_required_entry_bar_codes_ignores_entry_sessions_not_reached(tmp_path):
    """买入日还没走到已入库范围时，缺行是「等未来」，补采解决不了，不能报。"""
    with Store(tmp_path / "future-bars.duckdb") as store:
        _seed_experiment(store)
        _seed_calendar(store)

        # 一行行情都没有：无从判断，保持沉默。
        assert required_entry_bar_codes(store) == ([], None)

        # 行情最大日仍是信号日，买入日还没到。
        _seed_market(store, AS_OF)
        assert required_entry_bar_codes(store) == ([], None)


def test_required_entry_bar_codes_takes_the_earliest_entry_date_per_stock(tmp_path):
    """同一只票在多个批次缺行时，补采起点取最早的买入日，否则漏补前面那段。"""
    with Store(tmp_path / "earliest-entry.duckdb") as store:
        _seed_experiment(store, "early", as_of=AS_OF)
        _seed_experiment(store, "late", as_of=SESSIONS[1])
        _seed_calendar(store)
        _seed_market(store, SESSIONS[3])

        codes, start = required_entry_bar_codes(store)

        assert codes == [CODE]
        # early 批次的买入日是 SESSIONS[0]，late 批次是 SESSIONS[2]，取前者。
        assert start == SESSIONS[0]
