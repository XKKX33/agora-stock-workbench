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


def test_stocks_read_the_requested_batch_instead_of_always_the_latest(client, db_path):
    """传了 run_id 就必须读那一批，不能静默退回最新批次。

    侧栏的全局批次选择器把 run_id / as_of / strategy 带进来。原先 `StocksService.list()`
    硬编码 `latest_scan_rows()`，这三个参数连声明都没有，被 FastAPI 静默丢弃——
    用户切了批次候选池表格一动不动，也没有任何报错。与 /api/experiments 曾经的
    run_id 缺陷同源。
    """
    from engine.db import Store

    with Store(db_path, ensure_schema=False) as store:
        runs = store.con.execute(
            "SELECT run_id, as_of, strategy FROM scan_runs ORDER BY as_of DESC"
        ).fetchall()
    assert runs, "夹具应至少跑过一次扫描"
    run_id, as_of, strategy = runs[0]

    payload = client.get(
        "/api/stocks",
        params={"run_id": run_id, "as_of": as_of, "strategy": strategy, "per_page": 5},
    ).json()

    assert payload["run_id"] == run_id
    assert payload["as_of"] == as_of
    assert payload["items"]


def test_stocks_unknown_batch_fails_loudly_instead_of_showing_the_latest(client):
    """请求不存在的批次必须报错，绝不能退化成「忽略条件显示最新」。

    静默退回最新是最糟的形态：页面看着正常，用户以为在看自己选的批次。
    """
    response = client.get("/api/stocks", params={"run_id": "no-such-run"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "scan_not_found"
