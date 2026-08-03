from tests.test_run_scan_offline import AS_OF


def test_overview_uses_latest_scan_and_table_dates(client):
    response = client.get("/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_trade_date"] == AS_OF
    assert payload["latest_scan"]["scored_count"] == 7
    assert payload["tables"]["daily"]["row_count"] > 0
    assert payload["latest_scan"]["picks"]
