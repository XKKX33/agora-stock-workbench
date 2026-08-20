from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from engine.ai import AIRequestError
from engine.config import load_settings
from engine.db import Store
from tests.test_run_scan_offline import AS_OF as MARKET_AS_OF, _seed_db


AS_OF = "20260804"
STEP_NAMES = [
    "preflight",
    "calendar",
    "market_data",
    "backfill_returns",
    "integrity",
    "scan",
    "collect_news",
    "agents",
    "persist_experiment",
]


def _run_row(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "as_of": AS_OF,
        "data_cutoff_at": "2026-08-04T15:30:00+08:00",
        "status": "running",
        "strategy_name": "strong_mainup",
        "strategy_version": "strategy-v1",
        "model": "deepseekv4flash",
        "temperature": 0.2,
        "prompt_version": "agents-v1",
        "candidate_hash": "a" * 64,
        "candidate_count": 1,
        "final_count": 1,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": "2026-08-04T15:30:00+08:00",
        "finished_at": None,
        "error_json": None,
    }


class FakeOperations:
    def __init__(
        self,
        db_path: Path,
        *,
        fail_at: str | None = None,
        failure_message: str = "模型请求失败",
    ) -> None:
        self.db_path = db_path
        self.fail_at = fail_at
        self.failure_message = failure_message
        self.calls: list[str] = []

    def _done(self, name: str, context) -> dict:
        self.calls.append(name)
        if name == "calendar":
            context.as_of = AS_OF
        if name == "scan":
            with Store(self.db_path, ensure_schema=True) as store:
                store.create_experiment_run(_run_row(context.run_id))
        if name == self.fail_at:
            raise AIRequestError(self.failure_message)
        if name == "persist_experiment":
            return {
                "group_counts": {
                    "rule": 1,
                    "ai": 1,
                    "hybrid": 1,
                    "benchmark": 1,
                }
            }
        return {"name": name}

    def preflight(self, context) -> dict:
        return self._done("preflight", context)

    def calendar(self, context) -> dict:
        return self._done("calendar", context)

    def market_data(self, context) -> dict:
        return self._done("market_data", context)

    def backfill_returns(self, context) -> dict:
        return self._done("backfill_returns", context)

    def integrity(self, context) -> dict:
        return self._done("integrity", context)

    def scan(self, context) -> dict:
        return self._done("scan", context)

    def collect_news(self, context) -> dict:
        return self._done("collect_news", context)

    def agents(self, context) -> dict:
        return self._done("agents", context)

    def persist_experiment(self, context) -> dict:
        return self._done("persist_experiment", context)


def test_step_contract_exposes_fixed_order_and_metadata():
    from app.services.one_click import OneClickRunner

    contract = OneClickRunner.step_contract()

    assert [item["name"] for item in contract] == STEP_NAMES
    assert all(item["display_label"] for item in contract)
    assert all(item["required"] is True for item in contract)
    assert all(item["blocking"] is False for item in contract)
    assert all(isinstance(item["output_keys"], list) for item in contract)


def test_default_preflight_loads_workspace_settings(tmp_path: Path):
    from app.services.one_click import DefaultOneClickOperations, OneClickContext

    db_path = tmp_path / "preflight.duckdb"
    with Store(db_path, ensure_schema=True):
        pass

    result = DefaultOneClickOperations().preflight(
        OneClickContext(
            run_id="preflight-run",
            db_path=db_path,
            strategy="strong_mainup",
            trade_date=AS_OF,
            online=False,
            exchange="SSE",
        )
    )

    assert result["strategy"] == "strong_mainup"
    assert result["online"] is False


def test_one_click_runs_steps_in_fixed_order(tmp_path: Path):
    from app.services.one_click import OneClickRunner

    db_path = tmp_path / "market.duckdb"
    with Store(db_path, ensure_schema=True):
        pass
    operations = FakeOperations(db_path)

    result = OneClickRunner(db_path, operations=operations).run(
        run_id="r1",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
    )

    assert operations.calls == STEP_NAMES
    assert [step["name"] for step in result["steps"]] == STEP_NAMES
    assert result["current_step"] == "persist_experiment"
    assert result["as_of"] == AS_OF
    assert result["group_counts"] == {
        "rule": 1,
        "ai": 1,
        "hybrid": 1,
        "benchmark": 1,
    }


def test_ai_failure_becomes_warning_and_later_steps_continue(tmp_path: Path):
    from app.services.one_click import OneClickRunner

    db_path = tmp_path / "market.duckdb"
    with Store(db_path, ensure_schema=True):
        pass
    operations = FakeOperations(db_path, fail_at="agents")

    result = OneClickRunner(db_path, operations=operations).run(
        run_id="r1",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
    )

    assert operations.calls == STEP_NAMES
    assert result["has_warnings"] is True
    assert result["warning_count"] == 1
    agent_step = next(step for step in result["steps"] if step["name"] == "agents")
    assert agent_step["status"] == "warning"
    assert agent_step["data"]["error"]["type"] == "AIRequestError"
    assert result["steps"][-1]["status"] == "ok"


def test_failed_experiment_never_persists_bearer_secret(tmp_path: Path):
    from app.services.one_click import OneClickRunner

    db_path = tmp_path / "secret.duckdb"

    with Store(db_path, ensure_schema=True):
        pass
    operations = FakeOperations(
        db_path,
        fail_at="agents",
        failure_message="上游返回 Authorization: Bearer AUDIT_SECRET_SENTINEL",
    )

    result = OneClickRunner(db_path, operations=operations).run(
        run_id="secret-run",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
    )

    assert "AUDIT_SECRET_SENTINEL" not in str(result)


def test_one_click_agents_warn_when_pi_is_unavailable_without_legacy_runner(
    tmp_path: Path, monkeypatch
):
    import app.services.one_click as one_click
    from app.services.one_click import OneClickContext
    from engine.agents import AgentConfig

    db_path = tmp_path / "agent-unavailable.duckdb"
    with Store(db_path, ensure_schema=True):
        pass

    legacy_calls: list[object] = []
    monkeypatch.setattr(
        one_click,
        "run_judge",
        lambda *_args, **_kwargs: legacy_calls.append(object()),
        raising=False,
    )
    context = OneClickContext(
        run_id="pipeline-task-id",
        db_path=db_path,
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=False,
        exchange="SSE",
        as_of=AS_OF,
        agent_config=AgentConfig(
            default_candidates=1,
            default_depth=1,
            default_final=1,
            provider="openai-compatible",
            model="fake-model",
            reasoning_effort="low",
        ),
        scan_rows=pd.DataFrame([
            {"ts_code": "000001.SZ", "name": "示例", "industry": "测试", "total": 88.0}
        ]),
    )

    result = one_click.DefaultOneClickOperations().agents(context)

    assert result["_status"] == "warning"
    assert "Pi Agent 不可用" in result["_detail"]
    assert legacy_calls == []
    with Store(db_path, ensure_schema=False) as store:
        run = store.get_agent_run("pipeline-task-id")
        events = store.agent_events("pipeline-task-id")
    assert run is None
    assert events == []


def test_one_click_agents_skips_bad_candidate_and_persists_valid_report(
    tmp_path: Path, monkeypatch
):
    import app.services.one_click as one_click
    from app.schemas.pi_agent import PiJudgmentResult
    from app.services.one_click import OneClickContext
    from engine.agents import AgentConfig
    from engine.ai import AIConfig

    db_path = tmp_path / "agent-pi-workflow.duckdb"
    with Store(db_path, ensure_schema=True):
        pass

    class FakeRepository:
        def history(self, ts_code, _as_of, _limit):
            if ts_code == "000002.SZ":
                return pd.DataFrame()
            return pd.DataFrame([{"close": 10.0, "pct_chg": 1.0}])

    class FakeLoader:
        def __init__(self, _path):
            self.repository = FakeRepository()

        def _compact_row(self, row, _history):
            return {"ts_code": row["ts_code"], "name": row["name"], "industry": row["industry"]}

        def _load_snapshot(self, code, _as_of):
            return {"stock": {"ts_code": code, "name": "示例", "industry": "测试"}}

    class FakePiClient:
        def __init__(self):
            self.request = None
            self.run_id = None

        def start_judgment(self, request, *, run_id=None):
            self.request = request
            self.run_id = run_id
            return run_id

        def stream_events(self, run_id, after_seq=0):
            assert run_id == "pipeline-task-id"
            assert after_seq == 0
            yield {"source_seq": 1, "event_type": "stage.completed", "stage": "debate", "role": "bull", "data": {"summary": "多方依据"}}

        def get_result(self, run_id, request):
            assert run_id == "pipeline-task-id"
            return PiJudgmentResult.model_validate({
                "protocol_version": "1", "workflow_version": "1", "run_id": run_id,
                "trade_date": request.trade_date, "candidate_hash": request.candidate_hash,
                "input_hash": request.input_hash,
                "coarse": [{"ts_code": "000001.SZ", "rank": 1, "score": 80, "reason": "资金确认"}],
                "deep": [{"ts_code": "000001.SZ", "rank": 1, "score": 80, "analysts": {
                    "methodology": {"stance": "bull", "conclusion": "方法通过", "risks": []},
                    "sentiment": {"stance": "neutral", "conclusion": "舆情中性", "risks": []},
                    "trend": {"stance": "bull", "conclusion": "趋势向上", "risks": []},
                }}],
                "final": [{"ts_code": "000001.SZ", "rank": 1, "decision": "buy", "score": 80,
                           "bull_case": "多方依据", "bear_case": "空方风险", "rebuttal": "反驳", "risk_control": "止损"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            })

    fake_pi = FakePiClient()
    monkeypatch.setattr(one_click, "_AgentDataLoader", FakeLoader)
    monkeypatch.setattr(
        one_click,
        "run_judge",
        lambda *_args, **_kwargs: pytest.fail("不得调用 legacy run_judge"),
        raising=False,
    )
    context = OneClickContext(
        run_id="pipeline-task-id", db_path=db_path, strategy="strong_mainup", trade_date=AS_OF,
        online=False, exchange="SSE", as_of=AS_OF,
        agent_config=AgentConfig(default_candidates=2, default_depth=2, default_final=1,
                                 provider="openai-compatible", model=None, reasoning_effort="low"),
        ai_config=AIConfig(
            enabled=True,
            provider="openai-compatible",
            model="minimax-m3",
            base_url="https://example.test/v1",
        ),
        pi_client=fake_pi,
        scan_rows=pd.DataFrame([
            {"ts_code": "000001.SZ", "name": "示例", "industry": "测试", "total": 88.0},
            {"ts_code": "000002.SZ", "name": "缺历史", "industry": "测试", "total": 80.0},
        ]),
    )

    result = one_click.DefaultOneClickOperations().agents(context)

    assert result["candidates"] == 1
    assert result["_status"] == "warning"
    assert any("000002.SZ" in warning for warning in result["warnings"])
    assert fake_pi.run_id == "pipeline-task-id"
    assert fake_pi.request.mode == "batch"
    assert fake_pi.request.model.model == "minimax-m3"
    assert fake_pi.request.snapshots[0]["ts_code"] == "000001.SZ"
    assert fake_pi.request.input_hash
    with Store(db_path, ensure_schema=False) as store:
        run = store.get_agent_run("pipeline-task-id")
        events = store.agent_events("pipeline-task-id")
        judgments = store.agent_judgments("pipeline-task-id")
    assert run["status"] == "succeeded"
    assert [event["event_type"] for event in events] == ["run.started", "stage.completed", "run.completed"]
    assert list(judgments["run_id"]) == ["pipeline-task-id"]
    assert context.agent_result["final"][0]["ts_code"] == "000001.SZ"

def test_preflight_failure_warns_and_skips_dependent_steps_without_raising(
    tmp_path: Path,
):
    from app.services.one_click import OneClickRunner

    db_path = tmp_path / "market.duckdb"
    with Store(db_path, ensure_schema=True):
        pass

    result = OneClickRunner(
        db_path, operations=FakeOperations(db_path, fail_at="preflight")
    ).run(
        run_id="early-failure",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
    )

    assert len(result["steps"]) == len(STEP_NAMES)
    assert result["steps"][0]["status"] == "warning"
    assert all(step["status"] == "skipped" for step in result["steps"][1:])
    assert result["has_warnings"] is True


def test_missing_atomic_experiment_fails_after_all_steps(tmp_path: Path):
    from app.services.one_click import DefaultOneClickOperations, OneClickRunner
    from engine.experiments import candidate_pool_hash

    db_path = tmp_path / "progress-failure.duckdb"
    with Store(db_path, ensure_schema=True):
        pass

    class Operations(DefaultOneClickOperations):
        def __init__(self) -> None:
            self.scan_rows = pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "name": "示例",
                        "industry": "测试",
                        "rank": 1,
                        "total": 1.0,
                        "gate_reasons_json": "[]",
                        "contrib_json": "{}",
                        "money_class": "资金一致确认",
                    }
                ]
            )

        def preflight(self, context):
            return {}

        def calendar(self, context):
            context.as_of = AS_OF
            return {"as_of": AS_OF}

        def market_data(self, context):
            context.data_cutoff_at = "2026-08-04T15:30:00+08:00"
            return {"as_of": AS_OF}

        def backfill_returns(self, context):
            return {}

        def integrity(self, context):
            return {}

        def scan(self, context):
            row = _run_row(context.run_id)
            row["candidate_hash"] = candidate_pool_hash(self.scan_rows)
            context.scan_rows = self.scan_rows
            context.experiment_row = row
            with Store(db_path, ensure_schema=True) as store:
                store.create_experiment_run(row)
            return {"candidate_count": 1}

        def collect_news(self, context):
            return {}

        def agents(self, context):
            context.agent_result = {
                "deep": [{"ts_code": "000001.SZ", "score": 1.0}],
                "final": [{"ts_code": "000001.SZ", "score": 1.0}],
            }
            return {"final_count": 1}

        def persist_experiment(self, context):
            context.experiment_decisions = pd.DataFrame()
            return {"group_counts": {}}

    completed = []
    published = []

    def fail_on_final(progress):
        if progress["current_step"] == "persist_experiment":
            published.append(progress)
            raise RuntimeError("进度写入失败")

    with pytest.raises(RuntimeError, match="未产生可原子提交的实验结果"):
        OneClickRunner(db_path, operations=Operations()).run(
            run_id="progress-failure",
            strategy="strong_mainup",
            trade_date=AS_OF,
            online=True,
            exchange="SSE",
            on_step=fail_on_final,
            on_complete=lambda final_result, experiment: completed.append(
                (final_result, experiment)
            ),
        )

    with Store(db_path, ensure_schema=False) as store:
        run = store.experiment_run("progress-failure")
        decisions = store.experiment_decisions("progress-failure")
    persist_step = next(
        step for step in published[-1]["steps"] if step["name"] == "persist_experiment"
    )
    assert persist_step["status"] == "warning"
    assert "进度记录失败" in persist_step["detail"]
    assert completed == []
    assert run["status"] == "failed"
    assert decisions.empty


def test_scan_data_preparation_is_separate_from_integrity_and_scoring(tmp_path: Path):
    from engine.run_scan import (
        prepare_scan_data,
        score_prepared_scan,
        validate_scan_integrity,
    )

    db_path = tmp_path / "market.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)

    prepared = prepare_scan_data(
        strategy_name="strong_mainup",
        online=False,
        db_path=str(db_path),
    )
    integrity = validate_scan_integrity(prepared)
    result = score_prepared_scan(prepared, run_id="pipeline-task-id", record=False)

    assert integrity["as_of"] == prepared.as_of
    assert integrity["candidate_count"] == len(prepared.candidates)
    assert result.run_id == "pipeline-task-id"
    assert result.scored_count > 0


def test_one_click_scan_defers_scan_and_picks_until_final_commit(tmp_path: Path):
    from app.services.one_click import DefaultOneClickOperations, OneClickContext
    from engine.agents import AgentConfig
    from engine.ai import AIConfig

    prepared = _strict_preparation(tmp_path)
    context = OneClickContext(
        run_id="deferred-scan",
        db_path=Path(prepared.db_path),
        strategy=prepared.strategy_name,
        trade_date=prepared.as_of,
        online=False,
        exchange="SSE",
        prepared=prepared,
        data_cutoff_at=prepared.data_cutoff_at,
        strategy_config=prepared.strategy,
        agent_config=AgentConfig(
            enabled=True,
            default_candidates=20,
            default_depth=1,
            default_final=1,
            max_candidates=20,
            max_depth=1,
            max_final=1,
        ),
        ai_config=AIConfig(
            enabled=True,
            provider="openai_compatible",
            model="deepseek-v4-flash",
            base_url="https://api.pie-xian.com/v1",
        ),
        as_of=prepared.as_of,
    )

    DefaultOneClickOperations().scan(context)

    with Store(prepared.db_path, ensure_schema=False) as store:
        assert store.latest_scan_run().empty
        assert store.con.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 0
        assert store.experiment_run("deferred-scan")["status"] == "running"


def _strict_preparation(
    tmp_path: Path,
    *,
    missing_limit: bool = False,
    missing_daily: bool = False,
    mismatched_daily_codes: bool = False,
    missing_from_all_sources: bool = False,
    unexpected_in_all_sources: bool = False,
    suspended_from_all_sources: bool = False,
    missing_volume_ratio: bool = False,
    future_listed_without_snapshot: bool = False,
    extra_non_stock_limit: bool = False,
    short_history_code: str | None = None,
    expected_daily_rows: int | None = None,
):
    from engine.run_scan import prepare_scan_data

    db_path = tmp_path / "strict-market.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
        codes = [row[0] for row in store.con.execute("SELECT ts_code FROM stock_basic").fetchall()]
        if short_history_code is not None:
            store.con.execute(
                "DELETE FROM daily WHERE ts_code = ? AND trade_date NOT IN ("
                "SELECT trade_date FROM daily WHERE ts_code = ? "
                "ORDER BY trade_date DESC LIMIT 149)",
                [short_history_code, short_history_code],
            )
        if missing_limit:
            codes = codes[1:]
        limits = pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "trade_date": MARKET_AS_OF,
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
                for code in codes
            ]
        )
        store.upsert("daily_limit", limits, keys=("ts_code", "trade_date"))
        if missing_from_all_sources or suspended_from_all_sources:
            omitted_code = codes[-1]
            for table in ("daily", "daily_basic", "daily_limit"):
                store.con.execute(
                    f"DELETE FROM {table} WHERE ts_code = ? AND trade_date = ?",
                    [omitted_code, MARKET_AS_OF],
                )
            if suspended_from_all_sources:
                store.con.execute(
                    "INSERT INTO suspend_daily VALUES (?, ?)",
                    [omitted_code, MARKET_AS_OF],
                )
        if unexpected_in_all_sources:
            unexpected_code = "999999.SZ"
            for table, keys in (
                ("daily", ("ts_code", "trade_date")),
                ("daily_basic", ("ts_code", "trade_date")),
                ("daily_limit", ("ts_code", "trade_date")),
            ):
                row = store.con.execute(
                    f"SELECT * FROM {table} WHERE trade_date = ? LIMIT 1",
                    [MARKET_AS_OF],
                ).df()
                row["ts_code"] = unexpected_code
                store.upsert(table, row, keys=keys)
        if missing_daily:
            store.con.execute(
                "DELETE FROM daily WHERE ts_code = ? AND trade_date = ?",
                [codes[-1], MARKET_AS_OF],
            )
        if mismatched_daily_codes:
            store.con.execute(
                "UPDATE daily SET ts_code = ? "
                "WHERE ts_code = ? AND trade_date = ?",
                ["999999.SZ", codes[-1], MARKET_AS_OF],
            )
        if missing_volume_ratio:
            store.con.execute(
                "UPDATE daily_basic SET volume_ratio = NULL "
                "WHERE ts_code = ? AND trade_date = ?",
                [codes[-1], MARKET_AS_OF],
            )
        if future_listed_without_snapshot:
            store.con.execute(
                "INSERT INTO stock_basic VALUES "
                "('999998.SZ', '999998', '未来上市', '', '元器件', '主板', '20990101')"
            )
        if extra_non_stock_limit:
            store.con.execute(
                "INSERT INTO daily_limit VALUES "
                "('159001.SZ', ?, 1.1, 0.9)",
                [MARKET_AS_OF],
            )

    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0
    return prepare_scan_data(
        strategy_name="strong_mainup",
        online=False,
        db_path=str(db_path),
        settings_override=settings,
        expected_daily_rows=expected_daily_rows,
    )


def test_strict_integrity_reports_and_accepts_complete_source_coverage(tmp_path: Path):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path)
    audit = prepared.data_quality

    assert audit["source"] == "local_database"
    assert audit["as_of"] == MARKET_AS_OF
    assert audit["data_cutoff_at"]
    for name in ("daily", "stock_basic", "daily_basic", "daily_limit", "moneyflow"):
        metric = audit[name]
        assert metric["source"] == "local_database"
        assert metric["data_date"] == MARKET_AS_OF
        assert metric["rows"] > 0
        assert metric["coverage"] == 1.0
    assert audit["key_fields"]["missing_rate"] == 0.0

    integrity = validate_scan_integrity(prepared, require_complete_sources=True)
    assert integrity["data_quality"] == audit


def test_strict_integrity_warns_when_ingested_daily_shrinks_after_confirmation(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path)
    actual_rows = prepared.data_quality["daily"]["source_rows"]
    prepared = _strict_preparation(
        tmp_path,
        expected_daily_rows=actual_rows + 1,
    )

    result = validate_scan_integrity(prepared, require_complete_sources=True)

    assert prepared.data_quality["daily"]["confirmed_rows"] == actual_rows + 1
    assert any("确认行数" in warning for warning in result["warnings"])


def test_strict_integrity_uses_daily_as_historical_market_reference(tmp_path: Path):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, future_listed_without_snapshot=True)

    assert prepared.data_quality["daily"]["market_coverage"] == 1.0
    validate_scan_integrity(prepared, require_complete_sources=True)


def test_strict_integrity_allows_non_stock_rows_from_limit_endpoint(tmp_path: Path):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, extra_non_stock_limit=True)
    limit_quality = prepared.data_quality["daily_limit"]

    assert limit_quality["market_coverage"] == 1.0
    assert limit_quality["market_unexpected_sample"] == ["159001.SZ"]
    validate_scan_integrity(prepared, require_complete_sources=True)


def test_strict_integrity_accepts_missing_optional_volume_ratio(tmp_path: Path):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, missing_volume_ratio=True)

    assert prepared.data_quality["key_fields"]["missing_rate"] == 0.0
    volume_ratio = prepared.data_quality["optional_fields"]["volume_ratio"]
    assert volume_ratio["missing_count"] == 1
    assert volume_ratio["invalid_count"] == 0
    integrity = validate_scan_integrity(prepared, require_complete_sources=True)
    assert integrity["data_quality"] == prepared.data_quality


def test_strict_integrity_warns_for_partial_daily_limit_coverage(tmp_path: Path):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, missing_limit=True)

    result = validate_scan_integrity(prepared, require_complete_sources=True)

    assert any("daily_limit" in warning for warning in result["warnings"])


def test_strict_integrity_warns_when_peer_snapshots_disagree_with_daily(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, missing_daily=True)

    result = validate_scan_integrity(prepared, require_complete_sources=True)

    assert any("daily_basic 全市场覆盖率" in warning for warning in result["warnings"])


def test_strict_integrity_warns_for_equal_counts_with_different_stock_codes(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, mismatched_daily_codes=True)

    assert prepared.data_quality["daily"]["market_coverage"] < 1.0
    result = validate_scan_integrity(prepared, require_complete_sources=True)

    assert any("daily 全市场覆盖率" in warning for warning in result["warnings"])


def test_strict_integrity_accepts_source_aligned_absent_stock(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, suspended_from_all_sources=True)
    audit = prepared.data_quality

    for source_name in ("daily", "daily_basic", "daily_limit"):
        metric = audit[source_name]
        assert metric["market_expected_rows"] == audit["daily"]["rows"]
        assert len(metric["market_expected_sample"]) == metric["market_expected_rows"]
        assert metric["market_missing_count"] == 0
        assert metric["market_coverage"] == 1.0
    validate_scan_integrity(prepared, require_complete_sources=True)


def test_strict_integrity_accepts_code_absent_from_all_market_sources(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, missing_from_all_sources=True)
    audit = prepared.data_quality

    for source_name in ("daily", "daily_basic", "daily_limit"):
        metric = audit[source_name]
        assert metric["market_missing_count"] == 0
        assert metric["market_coverage"] == 1.0
    validate_scan_integrity(prepared, require_complete_sources=True)


def test_strict_integrity_warns_for_unlisted_code_present_in_all_market_sources(
    tmp_path: Path,
):
    from engine.run_scan import validate_scan_integrity

    prepared = _strict_preparation(tmp_path, unexpected_in_all_sources=True)
    audit = prepared.data_quality

    daily = audit["daily"]
    assert daily["market_invalid_count"] == 1
    assert daily["market_invalid_sample"] == ["999999.SZ"]
    assert daily["market_coverage"] < 1.0
    for source_name in ("daily_basic", "daily_limit"):
        assert audit[source_name]["market_coverage"] == 1.0
    result = validate_scan_integrity(prepared, require_complete_sources=True)

    assert any("daily 全市场覆盖率" in warning for warning in result["warnings"])


def test_one_click_warns_for_unavailable_news_source(tmp_path: Path):
    from app.services.one_click import (
        DefaultOneClickOperations,
        OneClickContext,
    )

    context = OneClickContext(
        run_id="news-unavailable",
        db_path=tmp_path / "market.duckdb",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
        settings={"news": {"enabled": False}},
        as_of=AS_OF,
    )

    result = DefaultOneClickOperations().collect_news(context)

    assert result["_status"] == "warning"
    assert "舆情采集不可用" in result["_detail"]


def test_one_click_warns_when_every_fetched_item_is_invalid(
    tmp_path: Path, monkeypatch
):
    import engine.close_pipeline as close_pipeline
    from app.services.one_click import DefaultOneClickOperations, OneClickContext

    context = OneClickContext(
        run_id="news-all-rejected",
        db_path=tmp_path / "market.duckdb",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=True,
        exchange="SSE",
        settings={"news": {"enabled": True}},
        as_of=AS_OF,
    )
    monkeypatch.setattr(
        close_pipeline,
        "_collect_news_step",
        lambda **_kwargs: close_pipeline.StepResult(
            name=close_pipeline.STEP_NEWS,
            status=close_pipeline.STATUS_OK,
            detail="采集 2 条,拒收 2 条",
            data={
                "fetched": 2,
                "stored": 0,
                "duplicates": 0,
                "rejected": [{"reason": "invalid"}, {"reason": "invalid"}],
            },
        ),
    )

    result = DefaultOneClickOperations().collect_news(context)

    assert result["_status"] == "warning"
    assert "全部被拒收" in result["_detail"]


_VIS_SESSIONS = [
    stamp.strftime("%Y%m%d") for stamp in pd.bdate_range("20260701", periods=25)
]
_VIS_SETTINGS = {"data": {"min_daily_rows": 0, "visibility_delay_sessions": 20}}


def _seed_visibility_calendar(db_path: Path, sessions: list[str]) -> None:
    """写入开市日历与逐日行情,让离线基准日与可见窗口都能算出来。"""
    with Store(db_path, ensure_schema=True) as store:
        store.upsert(
            "trade_cal",
            pd.DataFrame(
                [
                    {"exchange": "SSE", "cal_date": date, "is_open": 1}
                    for date in sessions
                ]
            ),
            keys=("exchange", "cal_date"),
        )
        store.upsert(
            "daily",
            pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": date,
                        "open": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "close": 10.0,
                    }
                    for date in sessions
                ]
            ),
            keys=("ts_code", "trade_date"),
        )


def _visibility_context(
    tmp_path: Path,
    *,
    sessions: list[str] | None = None,
    trade_date: str | None = None,
    online: bool = False,
    market_client: object | None = None,
):
    import app.services.one_click as one_click

    used = sessions if sessions is not None else _VIS_SESSIONS
    db_path = tmp_path / "visibility.duckdb"
    _seed_visibility_calendar(db_path, used)
    return one_click.OneClickContext(
        run_id="visibility",
        db_path=db_path,
        strategy="strong_mainup",
        trade_date=trade_date,
        online=online,
        exchange="SSE",
        settings=copy.deepcopy(_VIS_SETTINGS),
        market_client=market_client,
    )


def test_calendar_default_date_is_the_visible_session_not_the_latest(tmp_path: Path):
    import app.services.one_click as one_click

    context = _visibility_context(tmp_path)

    payload = one_click.DefaultOneClickOperations().calendar(context)

    assert payload["base_session"] == _VIS_SESSIONS[-1]
    assert payload["visible_as_of"] == _VIS_SESSIONS[-21]
    assert payload["as_of"] == _VIS_SESSIONS[-21]
    assert payload["delay_sessions"] == 20
    assert payload["hidden_count"] == 20
    assert payload["confirmed_rows"] is None
    assert context.as_of == _VIS_SESSIONS[-21]
    assert context.as_of != _VIS_SESSIONS[-1]
    assert context.ingest_as_of == _VIS_SESSIONS[-1]
    assert context.visibility_delay == 20
    assert context.hidden_sessions == tuple(_VIS_SESSIONS[-20:])
    assert payload["_detail"] == (
        f"确认信号日 {_VIS_SESSIONS[-21]}，可见上限 {_VIS_SESSIONS[-21]}，"
        f"基准日 {_VIS_SESSIONS[-1]}"
    )


def test_calendar_rejects_trade_date_inside_the_hidden_window(tmp_path: Path):
    import app.services.one_click as one_click
    from engine.visibility import LookaheadBlocked

    context = _visibility_context(tmp_path, trade_date=_VIS_SESSIONS[-5])

    with pytest.raises(LookaheadBlocked) as excinfo:
        one_click.DefaultOneClickOperations().calendar(context)

    assert excinfo.value.code == "lookahead_blocked"


def test_calendar_accepts_online_historical_date_within_visible_range(
    tmp_path: Path, monkeypatch
):
    import app.services.one_click as one_click

    requested = _VIS_SESSIONS[-22]
    context = _visibility_context(
        tmp_path, trade_date=requested, online=True, market_client=object()
    )
    monkeypatch.setattr(one_click, "ingest_calendar", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        one_click,
        "confirm_latest_trade_date",
        lambda *_args, **_kwargs: (_VIS_SESSIONS[-1], 5000),
    )

    payload = one_click.DefaultOneClickOperations().calendar(context)

    assert payload["as_of"] == requested
    assert payload["visible_as_of"] == _VIS_SESSIONS[-21]
    assert payload["base_session"] == _VIS_SESSIONS[-1]
    # 历史日期没有权威预期行数,不能沿用基准日的确认行数。
    assert payload["confirmed_rows"] is None
    assert context.confirmed_market_rows is None
    assert context.ingest_as_of == _VIS_SESSIONS[-1]


def test_calendar_blocks_when_calendar_history_is_shorter_than_the_window(
    tmp_path: Path,
):
    import app.services.one_click as one_click
    from engine.visibility import REASON_INSUFFICIENT, LookaheadBlocked

    context = _visibility_context(tmp_path, sessions=_VIS_SESSIONS[:5])

    with pytest.raises(LookaheadBlocked) as excinfo:
        one_click.DefaultOneClickOperations().calendar(context)

    assert excinfo.value.code == "visibility_window_unavailable"
    assert excinfo.value.reason == REASON_INSUFFICIENT


def test_market_data_ingests_the_base_session_while_scanning_the_visible_one(
    tmp_path: Path, monkeypatch
):
    from types import SimpleNamespace

    import app.services.one_click as one_click

    ingested: list[str] = []
    prepared_kwargs: dict = {}
    prepared = SimpleNamespace(
        strategy={"name": "strong_mainup"},
        as_of=_VIS_SESSIONS[-21],
        snapshot_count=1,
        candidates=pd.DataFrame([{"ts_code": "000001.SZ"}]),
        data_cutoff_at="2026-08-04T15:30:00+08:00",
        data_quality={"source": "local_database"},
    )
    monkeypatch.setattr(
        one_click,
        "ingest_snapshot",
        lambda _store, _client, as_of: ingested.append(as_of) or {"daily": 5000},
    )
    monkeypatch.setattr(
        one_click,
        "prepare_scan_data",
        lambda **kwargs: prepared_kwargs.update(kwargs) or prepared,
    )
    context = _visibility_context(tmp_path, online=True, market_client=object())
    context.as_of = _VIS_SESSIONS[-21]
    context.ingest_as_of = _VIS_SESSIONS[-1]
    context.visible_as_of = _VIS_SESSIONS[-21]

    payload = one_click.DefaultOneClickOperations().market_data(context)

    assert ingested == [_VIS_SESSIONS[-1]]
    assert prepared_kwargs["as_of"] == _VIS_SESSIONS[-21]
    assert payload["ingest_as_of"] == _VIS_SESSIONS[-1]
    assert payload["latest_ingested"] == {"daily": 5000}
    assert payload["_detail"] == (
        f"读取 {_VIS_SESSIONS[-21]} 行情快照 1 只，候选池 1 只"
    )


def test_market_data_skips_latest_ingest_when_refresh_is_disabled(
    tmp_path: Path, monkeypatch
):
    from types import SimpleNamespace

    import app.services.one_click as one_click

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("refresh_latest=False 时不应摄取最新交易日行情")

    monkeypatch.setattr(one_click, "ingest_snapshot", _forbidden)
    monkeypatch.setattr(
        one_click,
        "prepare_scan_data",
        lambda **_kwargs: SimpleNamespace(
            strategy={},
            as_of=_VIS_SESSIONS[-21],
            snapshot_count=1,
            candidates=pd.DataFrame([{"ts_code": "000001.SZ"}]),
            data_cutoff_at=None,
            data_quality={},
        ),
    )
    context = _visibility_context(tmp_path, online=True, market_client=object())
    context.as_of = _VIS_SESSIONS[-21]
    context.ingest_as_of = _VIS_SESSIONS[-1]
    context.refresh_latest = False

    payload = one_click.DefaultOneClickOperations().market_data(context)

    assert payload["latest_ingested"] is None


def test_backfill_returns_ignores_entry_dates_inside_the_hidden_window(
    tmp_path: Path, monkeypatch
):
    import app.services.one_click as one_click
    from engine.returns import ReturnsSummary

    calls: list[object] = []
    monkeypatch.setattr(
        one_click,
        "required_entry_limit_dates",
        lambda _store, _exchange: [_VIS_SESSIONS[-1]],
    )
    monkeypatch.setattr(
        one_click,
        "ingest_daily_limits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("隐藏窗口内的买入日不应触发涨跌停补采")
        ),
    )
    monkeypatch.setattr(
        one_click,
        "calculate_experiment_returns",
        lambda _store, *, exchange, visible_max: calls.append((exchange, visible_max))
        or ReturnsSummary(rows_written=0, filled=0, pending=1, unavailable=0),
    )
    context = _visibility_context(tmp_path)
    context.visible_as_of = _VIS_SESSIONS[-21]

    payload = one_click.DefaultOneClickOperations().backfill_returns(context)

    assert payload["required_limit_dates"] == []
    assert payload["daily_limit_rows"] == 0
    assert payload["visible_as_of"] == _VIS_SESSIONS[-21]
    assert calls == [("SSE", _VIS_SESSIONS[-21])]


def test_collect_news_skips_live_capture_for_historical_backfill(
    tmp_path: Path, monkeypatch
):
    import engine.close_pipeline as close_pipeline
    from app.services.one_click import DefaultOneClickOperations, OneClickContext

    def _forbidden(**_kwargs):
        raise AssertionError("历史补齐不应触发舆情采集")

    monkeypatch.setattr(close_pipeline, "_collect_news_step", _forbidden)
    context = OneClickContext(
        run_id="news-skipped",
        db_path=tmp_path / "market.duckdb",
        strategy="strong_mainup",
        trade_date=None,
        online=True,
        exchange="SSE",
        settings={"news": {"enabled": True}},
        as_of=_VIS_SESSIONS[-21],
        ingest_as_of=_VIS_SESSIONS[-1],
        collect_live_news=False,
    )

    payload = DefaultOneClickOperations().collect_news(context)

    assert payload["_status"] == "skipped"
    assert payload["reason"] == "historical_backfill"
    assert payload["trade_date"] == _VIS_SESSIONS[-21]
    assert payload["fetched"] == 0


def test_collect_news_files_live_sentiment_under_the_base_session(
    tmp_path: Path, monkeypatch
):
    import engine.close_pipeline as close_pipeline
    from app.services.one_click import DefaultOneClickOperations, OneClickContext

    captured: dict = {}
    monkeypatch.setattr(
        close_pipeline,
        "_collect_news_step",
        lambda **kwargs: captured.update(kwargs)
        or close_pipeline.StepResult(
            name=close_pipeline.STEP_NEWS,
            status=close_pipeline.STATUS_OK,
            detail="采集 1 条",
            data={"fetched": 1, "stored": 1, "duplicates": 0},
        ),
    )
    context = OneClickContext(
        run_id="news-live",
        db_path=tmp_path / "market.duckdb",
        strategy="strong_mainup",
        trade_date=None,
        online=True,
        exchange="SSE",
        settings={"news": {"enabled": True}},
        as_of=_VIS_SESSIONS[-21],
        ingest_as_of=_VIS_SESSIONS[-1],
    )

    payload = DefaultOneClickOperations().collect_news(context)

    # 实时热榜属于基准日,不能挂到 20 个交易日前的历史批次上。
    assert captured["trade_date"] == _VIS_SESSIONS[-1]
    assert payload["stored"] == 1


def test_runner_forwards_backfill_switches_into_the_context(tmp_path: Path):
    from app.services.one_click import OneClickRunner

    db_path = tmp_path / "market.duckdb"
    with Store(db_path, ensure_schema=True):
        pass
    seen: list[tuple[bool, bool]] = []

    class Recording(FakeOperations):
        def preflight(self, context) -> dict:
            seen.append((context.refresh_latest, context.collect_live_news))
            return super().preflight(context)

    OneClickRunner(db_path, operations=Recording(db_path)).run(
        run_id="switches",
        strategy="strong_mainup",
        trade_date=AS_OF,
        online=False,
        exchange="SSE",
        refresh_latest=False,
        collect_live_news=False,
    )

    assert seen == [(False, False)]
def test_data_quality_exposes_target_max_coverage_gaps_and_history_window(tmp_path: Path):
    """扫描审计必须能解释目标日、最大日期、覆盖率和历史窗口。"""
    prepared = _strict_preparation(tmp_path)
    quality = prepared.data_quality

    assert quality["target_date"] == MARKET_AS_OF
    assert quality["max_date"] == MARKET_AS_OF
    assert quality["missing_tables"] == []
    assert quality["missing_dates"] == {}
    assert quality["coverage_stock_count"] == quality["daily"]["market_expected_rows"]
    assert quality["coverage_rate"] == 1.0
    assert quality["daily_limit_coverage"] == 1.0
    assert quality["history_window"]["required_bars"] == prepared.minimum_daily_rows or quality["history_window"]["required_bars"] > 0
    assert quality["history_window"]["satisfied"] is True


def test_prepare_scan_data_excludes_only_candidates_below_history_requirement(
    tmp_path: Path,
):
    short_code = "600040.SH"

    prepared = _strict_preparation(tmp_path, short_history_code=short_code)
    history_window = prepared.data_quality["history_window"]

    assert short_code not in set(prepared.candidates["ts_code"].astype(str))
    assert short_code not in {context.ts_code for context in prepared.contexts}
    assert history_window["required_bars"] == 150
    assert history_window["evaluated_count"] == 7
    assert history_window["eligible_count"] == 6
    assert history_window["excluded_count"] == 1
    assert history_window["excluded"] == [{"ts_code": short_code, "bars": 149}]
    assert history_window["satisfied"] is False


def test_online_history_backfill_attempts_candidates_below_full_requirement(
    tmp_path: Path, monkeypatch
):
    import engine.run_scan as run_scan

    code = "600040.SH"
    db_path = tmp_path / "history-backfill.duckdb"
    calls: list[tuple[list[str], str, str]] = []
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
        store.con.execute(
            "DELETE FROM daily WHERE ts_code = ? AND trade_date NOT IN ("
            "SELECT trade_date FROM daily WHERE ts_code = ? "
            "ORDER BY trade_date DESC LIMIT 149)",
            [code, code],
        )
        monkeypatch.setattr(
            run_scan,
            "ingest_history",
            lambda _store, _client, codes, start, end: (
                calls.append((list(codes), start, end)) or 1
            ),
        )

        rows = run_scan._backfill_history(
            store,
            object(),
            pd.DataFrame([{"ts_code": code}]),
            MARKET_AS_OF,
            150,
        )

    assert rows == 1
    assert calls and calls[0][0] == [code]
    assert calls[0][2] == MARKET_AS_OF


def test_prepare_scan_data_freezes_only_candidates_with_scoring_contexts(
    tmp_path: Path, monkeypatch
):
    import engine.run_scan as run_scan

    db_path = tmp_path / "context-candidates.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0
    original_build_contexts = run_scan._build_contexts
    dropped_codes: list[str] = []

    def build_without_last_context(*args, **kwargs):
        contexts = original_build_contexts(*args, **kwargs)
        dropped_codes.append(contexts[-1].ts_code)
        return contexts[:-1]

    monkeypatch.setattr(run_scan, "_build_contexts", build_without_last_context)

    prepared = run_scan.prepare_scan_data(
        strategy_name="strong_mainup",
        online=False,
        db_path=str(db_path),
        settings_override=settings,
    )

    candidate_codes = set(prepared.candidates["ts_code"].astype(str))
    context_codes = {context.ts_code for context in prepared.contexts}
    assert candidate_codes == context_codes
    assert dropped_codes[0] not in candidate_codes
    assert prepared.data_quality["context_filter"] == {
        "evaluated_count": 7,
        "eligible_count": 6,
        "excluded_count": 1,
        "excluded_codes": dropped_codes,
    }


def test_one_click_market_data_continues_with_eligible_history_candidates(
    tmp_path: Path,
):
    from app.services.one_click import DefaultOneClickOperations, OneClickContext

    prepared = _strict_preparation(tmp_path, short_history_code="600040.SH")
    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0
    context = OneClickContext(
        run_id="partial-history",
        db_path=Path(prepared.db_path),
        strategy="strong_mainup",
        trade_date=MARKET_AS_OF,
        online=False,
        exchange="SSE",
        settings=settings,
        as_of=MARKET_AS_OF,
        visible_as_of=MARKET_AS_OF,
    )

    payload = DefaultOneClickOperations().market_data(context)

    assert context.prepared is not None
    assert payload["candidate_pool_count"] == 7
    assert payload["candidate_count"] == 6
    assert payload["excluded_candidate_count"] == 1
    assert "历史不足排除 1 只" in payload["_detail"]


def test_one_click_market_data_reports_all_candidate_exclusion_reasons(
    tmp_path: Path, monkeypatch
):
    from types import SimpleNamespace

    import app.services.one_click as one_click

    prepared = SimpleNamespace(
        strategy={},
        as_of=MARKET_AS_OF,
        snapshot_count=7,
        candidates=pd.DataFrame(
            [{"ts_code": f"60000{index}.SH"} for index in range(5)]
        ),
        data_cutoff_at=None,
        data_quality={
            "candidate_pool_count": 7,
            "history_window": {"excluded_count": 1},
            "context_filter": {"excluded_count": 1},
        },
    )
    monkeypatch.setattr(one_click, "prepare_scan_data", lambda **_kwargs: prepared)
    context = one_click.OneClickContext(
        run_id="candidate-exclusions",
        db_path=tmp_path / "unused.duckdb",
        strategy="strong_mainup",
        trade_date=MARKET_AS_OF,
        online=False,
        exchange="SSE",
        settings={},
        as_of=MARKET_AS_OF,
        visible_as_of=MARKET_AS_OF,
    )

    payload = one_click.DefaultOneClickOperations().market_data(context)

    assert payload["excluded_candidate_count"] == 2
    assert payload["history_excluded_candidate_count"] == 1
    assert payload["context_excluded_candidate_count"] == 1
    assert "其他条件排除 1 只" in payload["_detail"]


def test_one_click_market_data_warns_when_target_limit_prices_are_missing(tmp_path: Path):
    """涨跌停价缺失要显式告警，但不能拖停全部候选。"""
    import copy
    from app.services.one_click import DefaultOneClickOperations, OneClickContext
    from engine.config import load_settings

    db_path = tmp_path / "missing-limit-market.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
        store.con.execute(
            "DELETE FROM daily_limit WHERE trade_date = ?", [MARKET_AS_OF]
        )
    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0
    context = OneClickContext(
        run_id="missing-limit",
        db_path=db_path,
        strategy="strong_mainup",
        trade_date=MARKET_AS_OF,
        online=False,
        exchange="SSE",
        settings=settings,
        as_of=MARKET_AS_OF,
        visible_as_of=MARKET_AS_OF,
    )

    result = DefaultOneClickOperations().market_data(context)

    assert result["_status"] == "warning"
    assert any("daily_limit" in warning for warning in result["warnings"])
def test_prepare_scan_data_rejects_missing_required_table(tmp_path: Path):
    from engine.run_scan import prepare_scan_data

    db_path = tmp_path / "missing-daily-basic.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
        store.con.execute("DROP TABLE daily_basic")
    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0

    with pytest.raises(RuntimeError, match="daily_basic"):
        prepare_scan_data(
            strategy_name="strong_mainup",
            online=False,
            db_path=str(db_path),
            settings_override=settings,
        )


def test_market_data_reports_low_source_coverage_without_aborting_integrity(tmp_path: Path):
    """覆盖率不足记录质量警告，由完整上下文决定哪些股票可评分。"""
    import copy
    from engine.run_scan import prepare_scan_data

    db_path = tmp_path / "low-coverage.duckdb"
    with Store(db_path, ensure_schema=True) as store:
        _seed_db(store)
        store.con.execute(
            "DELETE FROM daily_limit WHERE ts_code = (SELECT MIN(ts_code) FROM daily_limit)"
        )
    settings = copy.deepcopy(load_settings())
    settings["data"]["min_daily_rows"] = 0
    prepared = prepare_scan_data(
        strategy_name="strong_mainup",
        online=False,
        db_path=str(db_path),
        settings_override=settings,
        as_of=MARKET_AS_OF,
    )
    assert prepared.data_quality["daily_limit_coverage"] < 1.0
    from engine.run_scan import validate_scan_integrity

    result = validate_scan_integrity(prepared, require_complete_sources=True)
    assert any("daily_limit" in warning for warning in result["warnings"])
