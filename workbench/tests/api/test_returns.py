from __future__ import annotations

import pandas as pd

from engine.config import load_settings
from engine.db import Store
from engine.returns import calculate_experiment_returns
from engine.visibility import load_delay_sessions
from app.services.returns import ReturnsService


def test_returns_service_detail_and_summary_use_independent_table(tmp_path):
    path = tmp_path / "returns-api.duckdb"
    with Store(path) as store:
        run = {
            "run_id": "r", "as_of": "20260804", "data_cutoff_at": "20260804",
            "status": "queued", "strategy_name": "test", "strategy_version": "1",
            "model": "test", "temperature": 0.0, "prompt_version": "v1",
            "candidate_hash": "h", "candidate_count": 1, "final_count": 1,
            "hybrid_rule_weight": 0.5, "hybrid_ai_weight": 0.5,
            "created_at": "now", "finished_at": "now", "error_json": None,
        }
        decisions = pd.DataFrame([{"run_id": "r", "group_name": group, "ts_code": "000001.SZ", "name": "x", "rank": 1} for group in ("rule", "ai", "hybrid", "benchmark")])
        store.record_experiment(run, decisions)
        store.upsert("trade_cal", pd.DataFrame([{"exchange": "SSE", "cal_date": date, "is_open": 1} for date in ("20260804", "20260805")]), keys=("exchange", "cal_date"))
        store.upsert("daily", pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260805", "open": 10.0, "high": 10.5, "low": 9.5, "close": 11.0}]), keys=("ts_code", "trade_date"))
        store.upsert("daily_limit", pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260805", "up_limit": 11.0, "down_limit": 9.0}]), keys=("ts_code", "trade_date"))
        calculate_experiment_returns(store, run_id="r")
    service = ReturnsService(path)
    detail = service.detail(run_id="r", group_name="rule", horizon="t1_close")
    assert detail["total"] == 1
    assert detail["items"][0]["gross_return"] == 0.1
    summary = service.summary(run_id="r")
    assert summary["groups"]["rule"]["t1_close"]["available"] is True
    assert summary["groups"]["rule"]["t1_close"]["items"][0]["gross_return"] == 0.1


def test_calculate_response_exposes_visible_as_of(client, db_path):
    """接口必须回传可见日,前端才能解释"为什么最近的日期还没有收益"。"""
    settings = load_settings()
    delay = load_delay_sessions(settings)
    with Store(db_path, ensure_schema=False) as store:
        base = store.latest_confirmed_date(int(settings["data"]["min_daily_rows"])) or store.latest_date()
        expected_visible = store.open_dates("SSE", base, delay + 1)[0]

    response = client.post("/api/returns/calculate")

    assert response.status_code == 202
    payload = response.json()
    assert payload["delay_sessions"] == delay
    assert payload["visible_as_of"] == expected_visible
    assert payload["visible_as_of"] < base
