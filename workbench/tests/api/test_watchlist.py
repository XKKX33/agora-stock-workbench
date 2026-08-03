"""自选股接口隔离测试:添加/列表/筛选/删除,使用 conftest 的隔离库。"""

from __future__ import annotations


def _codes(items: list[dict]) -> list[str]:
    return [item["ts_code"] for item in items]


def test_watchlist_empty_returns_empty_list(client):
    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"] == []
    assert payload["meta"]["total"] == 0
    assert payload["meta"]["pages"] == 0


def test_watchlist_add_unknown_stock_returns_404(client):
    resp = client.post("/api/watchlist", json={"ts_code": "999999.SH"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "stock_not_found"


def test_watchlist_add_requires_ts_code(client):
    resp = client.post("/api/watchlist", json={"ts_code": "  "})
    assert resp.status_code == 400


def test_watchlist_add_list_remove_roundtrip(client):
    # 添加两只,列表应带各自最新行情
    for code in ("600001.SH", "600030.SH"):
        resp = client.post("/api/watchlist", json={"ts_code": code})
        assert resp.status_code == 200
        assert resp.json()["added"] is True

    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["total"] == 2
    items = {item["ts_code"]: item for item in payload["items"]}
    assert set(items) == {"600001.SH", "600030.SH"}
    first = items["600001.SH"]
    assert first["name"] == "强主升A"
    assert first["industry"] == "半导体"
    assert first["close"] is not None
    assert first["pct_chg"] is not None
    assert first["last_date"] == "20250812"

    # 重复添加幂等:总数不变
    resp = client.post("/api/watchlist", json={"ts_code": "600001.SH"})
    assert resp.status_code == 200
    assert client.get("/api/watchlist").json()["meta"]["total"] == 2

    # 删除一只
    resp = client.delete("/api/watchlist/600001.SH")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert _codes(client.get("/api/watchlist").json()["items"]) == ["600030.SH"]

    # 删除不存在的也幂等返回 200
    resp = client.delete("/api/watchlist/600001.SH")
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


def test_watchlist_search_and_industry_filter(client):
    for code in ("600001.SH", "600030.SH", "600020.SH"):
        client.post("/api/watchlist", json={"ts_code": code})

    resp = client.get("/api/watchlist", params={"search": "强主升"})
    assert _codes(resp.json()["items"]) == ["600001.SH"]

    resp = client.get("/api/watchlist", params={"search": "6000"})
    assert len(resp.json()["items"]) == 3

    resp = client.get("/api/watchlist", params={"industry": "消费电子"})
    assert _codes(resp.json()["items"]) == ["600030.SH"]

    resp = client.get("/api/watchlist", params={"industry": "半导体", "search": "600020"})
    assert resp.json()["items"] == []
