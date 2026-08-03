def test_stocks_filter_by_passed_and_industry(client):
    response = client.get(
        "/api/stocks",
        params={"passed": True, "industry": "半导体", "per_page": 50},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    assert all(item["passed"] for item in payload["items"])
    assert all(item["industry"] == "半导体" for item in payload["items"])


def test_stock_detail_contains_factor_trace(client):
    response = client.get("/api/stocks/600001.SH")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ts_code"] == "600001.SH"
    assert payload["factors"]
    assert "gate_reasons" in payload
    assert payload["history"]


def test_stock_sort_rejects_unknown_column(client):
    response = client.get("/api/stocks", params={"sort": "drop table"})

    assert response.status_code == 422
