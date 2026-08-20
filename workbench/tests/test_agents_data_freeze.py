from __future__ import annotations

import pandas as pd
import pytest

from app.services.agents_data import AgentDataMixin, FrozenAgentInput


class _Repository:
    def __init__(self, frame: pd.DataFrame, *, history_error: Exception | None = None):
        self.frame = frame
        self.history_error = history_error
        self.history_calls: list[tuple[str, str, int]] = []
        self.exact_calls: list[tuple[str, str | None, str | None]] = []
    def scan_rows_exact(self, run_id: str, as_of: str | None = None, strategy: str | None = None):
        self.exact_calls.append((run_id, as_of, strategy))
        if run_id != "scan-1":
            raise RuntimeError("扫描批次 missing-run 不存在")
        if as_of != "20260813":
            raise RuntimeError("扫描批次日期不匹配")
        return {"run_id": "scan-1", "as_of": "20260813", "strategy": "strong"}, self.frame
    def latest_scan_rows(self):
        return {"run_id": "scan-1", "as_of": "20260813"}, self.frame
    def history(self, ts_code: str, as_of: str, bars: int = 120):
        self.history_calls.append((ts_code, as_of, bars))
        if self.history_error:
            raise self.history_error
        return pd.DataFrame(
            [
                {
                    "trade_date": as_of,
                    "close": 10.0,
                    "pct_chg": 1.0,
                    "high": 10.2,
                    "low": 9.8,
                    "vol": 100,
                    "amount": 1000,
                }
            ]
        )


class _Loader(AgentDataMixin):
    def __init__(self, repository: _Repository, snapshots: dict[str, dict] | None = None):
        self.repository = repository
        self.db_path = None
        self.snapshots = snapshots or {}
        self.snapshot_calls: list[tuple[str, str]] = []

    def _load_snapshot(self, ts_code: str, as_of: str) -> dict:
        self.snapshot_calls.append((ts_code, as_of))
        return self.snapshots[ts_code]


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "money_class": "确认", "total": 88.0},
            {"ts_code": "000002.SZ", "name": "万科A", "industry": "地产", "money_class": "观察", "total": 77.0},
        ]
    )


def _snapshot(code: str) -> dict:
    return {
        "stock": {"ts_code": code, "name": "示例", "industry": "行业", "close": 10.0},
        "daily": {},
        "weekly": {},
        "moneyflow": {"recent": [], "net_sum_5": None},
        "news": {"stock_items": [], "industry_items": [], "source_note": "frozen"},
    }


def test_freeze_agent_input_reads_every_candidate_and_returns_hashes():
    repository = _Repository(_frame())
    loader = _Loader(repository, {"000001.SZ": _snapshot("000001.SZ"), "000002.SZ": _snapshot("000002.SZ")})

    frozen = loader.freeze_agent_input(candidates_n=2, ts_codes=None, as_of="20260813")

    assert isinstance(frozen, FrozenAgentInput)
    assert [row["ts_code"] for row in frozen.candidates] == ["000001.SZ", "000002.SZ"]
    assert [row["ts_code"] for row in frozen.snapshots] == ["000001.SZ", "000002.SZ"]
    assert [row["stock"]["ts_code"] for row in frozen.snapshots] == ["000001.SZ", "000002.SZ"]
    assert len(frozen.candidate_hash) == 64
    assert len(frozen.input_hash) == 64
    assert repository.history_calls == [("000001.SZ", "20260813", 150), ("000002.SZ", "20260813", 150)]
    assert loader.snapshot_calls == [("000001.SZ", "20260813"), ("000002.SZ", "20260813")]


def test_freeze_agent_input_fails_loudly_when_history_read_fails():
    repository = _Repository(_frame(), history_error=RuntimeError("行情源断开"))
    loader = _Loader(repository, {"000001.SZ": _snapshot("000001.SZ")})

    with pytest.raises(RuntimeError, match="行情源断开"):
        loader.freeze_agent_input(candidates_n=1, ts_codes=None, as_of="20260813")
    assert loader.snapshot_calls == []


def test_freeze_agent_input_rejects_empty_history_and_bad_snapshot():
    repository = _Repository(_frame())
    loader = _Loader(repository, {"000001.SZ": {"stock": {"ts_code": "999999.SZ"}}})

    class EmptyHistory(_Repository):
        def history(self, ts_code: str, as_of: str, bars: int = 120):
            self.history_calls.append((ts_code, as_of, bars))
            return pd.DataFrame()

    empty_loader = _Loader(EmptyHistory(_frame()), {"000001.SZ": _snapshot("000001.SZ")})
    with pytest.raises(RuntimeError, match="缺少 Agent 粗筛所需历史行情"):
        empty_loader.freeze_agent_input(candidates_n=1, ts_codes=None, as_of="20260813")

    with pytest.raises(RuntimeError, match="完整快照.*000001.SZ"):
        loader.freeze_agent_input(candidates_n=1, ts_codes=None, as_of="20260813")


def test_freeze_agent_input_rejects_requested_code_outside_frozen_scan_pool():
    loader = _Loader(_Repository(_frame()), {"999999.SZ": _snapshot("999999.SZ")})
    with pytest.raises(RuntimeError, match="不在扫描候选池"):
        loader.freeze_agent_input(candidates_n=2, ts_codes=["999999.SZ"], as_of="20260813")
def test_freeze_agent_input_fixed_run_id_does_not_fallback_to_latest():
    repository = _Repository(_frame())
    repository.exact_calls = []
    loader = _Loader(repository, {"000001.SZ": _snapshot("000001.SZ")})
    with pytest.raises(RuntimeError, match="扫描批次.*不存在"):
        loader.freeze_agent_input(
            candidates_n=1,
            ts_codes=None,
            as_of="20260813",
            run_id="missing-run",
        )
    assert repository.exact_calls == [("missing-run", "20260813", None)]


def test_freeze_agent_input_fixed_run_id_rejects_date_mismatch():
    repository = _Repository(_frame())
    repository.exact_calls = []
    loader = _Loader(repository, {"000001.SZ": _snapshot("000001.SZ")})
    with pytest.raises(RuntimeError, match="日期不匹配"):
        loader.freeze_agent_input(
            candidates_n=1,
            ts_codes=None,
            as_of="20260814",
            run_id="scan-1",
        )


def test_freeze_agent_input_hash_is_stable_for_same_explicit_batch():
    repository = _Repository(_frame())
    repository.exact_calls = []
    loader = _Loader(repository, {"000001.SZ": _snapshot("000001.SZ")})
    first = loader.freeze_agent_input(1, None, "20260813", run_id="scan-1")
    second = loader.freeze_agent_input(1, None, "20260813", run_id="scan-1")
    assert first.scan_run_id == second.scan_run_id == "scan-1"
    assert first.candidate_hash == second.candidate_hash
    assert first.input_hash == second.input_hash
def test_freeze_agent_input_rejects_hidden_as_of_at_freeze_boundary():
    loader = _Loader(_Repository(_frame()), {"000001.SZ": _snapshot("000001.SZ")})
    loader._ensure_visible_as_of = lambda value: (_ for _ in ()).throw(
        RuntimeError("lookahead_blocked")
    )
    with pytest.raises(RuntimeError, match="lookahead_blocked"):
        loader.freeze_agent_input(1, None, "20260814")


def test_frozen_input_is_independent_from_later_repository_mutation():
    repository = _Repository(_frame())
    snapshot = _snapshot("000001.SZ")
    loader = _Loader(repository, {"000001.SZ": snapshot})
    frozen = loader.freeze_agent_input(1, None, "20260813")
    original_hash = frozen.input_hash
    snapshot["stock"]["name"] = "后来被修改"
    repository.frame.loc[0, "total"] = 999
    assert frozen.input_hash == original_hash
    assert frozen.snapshots[0]["stock"].get("name") != "后来被修改"
def test_freeze_agent_input_rejects_more_than_top20_candidates():
    loader = _Loader(_Repository(_frame()), {"000001.SZ": _snapshot("000001.SZ")})
    with pytest.raises(ValueError, match="1~20"):
        loader.freeze_agent_input(candidates_n=21, ts_codes=None, as_of="20260813")
