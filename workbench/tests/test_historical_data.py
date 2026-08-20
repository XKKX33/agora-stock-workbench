from __future__ import annotations

from pathlib import Path

import pytest

import engine.historical_data as historical_data


def test_prepare_historical_data_delegates_to_scan_preparation_without_scoring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    class Prepared:
        as_of = "20260714"
        strategy_name = "strong_mainup"
        snapshot_count = 5524
        candidates = [1, 2, 3]
        contexts = [1, 2]
        data_cutoff_at = "2026-08-16T00:00:00+00:00"
        data_quality = {"history_window": {"satisfied": True}}

    def fake_prepare_scan_data(**kwargs):
        calls.append(kwargs)
        return Prepared()

    monkeypatch.setattr(historical_data, "prepare_scan_data", fake_prepare_scan_data)
    monkeypatch.setattr(
        historical_data,
        "validate_scan_integrity",
        lambda *args, **kwargs: {"ok": True},
    )

    result = historical_data.prepare_historical_data(
        db_path=tmp_path / "market.duckdb",
        trade_date="20260714",
        strategy_name="strong_mainup",
        online=True,
    )

    assert calls == [
        {
            "strategy_name": "strong_mainup",
            "online": True,
            "db_path": str(tmp_path / "market.duckdb"),
            "as_of": "20260714",
        }
    ]
    assert result == {
        "as_of": "20260714",
        "strategy": "strong_mainup",
        "snapshot_count": 5524,
        "candidate_count": 3,
        "context_count": 2,
        "data_cutoff_at": "2026-08-16T00:00:00+00:00",
        "data_quality": {"history_window": {"satisfied": True}},
        "integrity": {"ok": True},
    }

def test_prepare_historical_data_rejects_missing_trade_date() -> None:
    with pytest.raises(ValueError, match="trade_date"):
        historical_data.prepare_historical_data(
            db_path="market.duckdb", trade_date="", online=False
        )
