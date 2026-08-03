"""全市场筛选接口隔离测试,使用 conftest 的隔离库。"""

from __future__ import annotations


def test_default_list_sorted_by_pct_chg(client):
    resp = client.get("/api/screener")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["as_of"] == "20250812"
    assert payload["meta"] == {"page": 1, "per_page": 30, "total": 7, "pages": 1}
    items = payload["items"]
    assert len(items) == 7
    assert items[0]["ts_code"] == "600030.SH"  # 爆量涨停日涨幅最大
    fields = (
        "ts_code", "name", "industry", "close", "pct_chg",
        "vol_ratio", "turnover_rate", "rsi6", "total_mv", "circ_mv",
    )
    for item in items:
        for key in fields:
            assert key in item


def test_industry_filter(client):
    resp = client.get("/api/screener", params={"industry": "半导体"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["total"] == 3
    assert {item["industry"] for item in payload["items"]} == {"半导体"}


def test_pct_filter_hits_blowoff_stock(client):
    resp = client.get("/api/screener", params={"pct_min": 9.0, "pct_max": 9.9})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["total"] == 1
    assert payload["items"][0]["ts_code"] == "600030.SH"
    assert 9.0 <= payload["items"][0]["pct_chg"] <= 9.9


def test_vol_ratio_min_filter(client):
    resp = client.get("/api/screener", params={"vol_ratio_min": 2.0})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["total"] == 1
    assert payload["items"][0]["ts_code"] == "600030.SH"
    assert payload["items"][0]["vol_ratio"] >= 2.0


def test_pagination(client):
    resp = client.get("/api/screener", params={"per_page": 3, "page": 2})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"] == {"page": 2, "per_page": 3, "total": 7, "pages": 3}
    assert len(payload["items"]) == 3
    resp = client.get("/api/screener", params={"per_page": 3, "page": 3})
    assert len(resp.json()["items"]) == 1


def test_sort_close_both_orders(client):
    asc = client.get("/api/screener", params={"sort": "close", "order": "asc"}).json()["items"]
    desc = client.get("/api/screener", params={"sort": "close", "order": "desc"}).json()["items"]
    asc_closes = [item["close"] for item in asc]
    desc_closes = [item["close"] for item in desc]
    assert asc_closes == sorted(asc_closes)
    assert desc_closes == sorted(desc_closes, reverse=True)
    assert asc_closes[0] != desc_closes[0]


def test_sort_rsi6(client):
    resp = client.get("/api/screener", params={"sort": "rsi6", "order": "asc"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    rsi = [item["rsi6"] for item in items]
    assert all(value is not None for value in rsi)
    assert rsi == sorted(rsi)


def test_invalid_params_rejected(client):
    assert client.get("/api/screener", params={"per_page": 201}).status_code == 422
    assert client.get("/api/screener", params={"page": 0}).status_code == 422
    assert client.get("/api/screener", params={"sort": "bogus"}).status_code == 422
    assert client.get("/api/screener", params={"order": "up"}).status_code == 422