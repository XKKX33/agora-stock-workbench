"""盘后任务链 API:手动触发、幂等重跑、闸门拒绝与状态查询。

这里的固定装置(tests/api/conftest.py)灌的是 2025 年的合成行情,
交易日历也只覆盖到那一段。因此:

- 不带 trade_date 的自动触发一定被闸门以 `calendar_stale` 拒绝——
  这正是"日历没覆盖到今天就不猜"的期望行为,不是测试环境的缺陷。
- 完整离线闭环走手动指定 trade_date 的路径,它只校验目标日是开市日。
"""

from __future__ import annotations

import time

import pytest

from tests.test_run_scan_offline import AS_OF


def _post_pipeline(client, **overrides) -> object:
    body: dict = {"online": False}
    body.update(overrides)
    return client.post("/api/pipelines", json=body)


def _wait_for_pipeline(client, job_id: str, timeout: float = 60.0) -> dict:
    """等待链条跑完。超时直接失败,不返回中间态让断言误判成功。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/pipelines/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"盘后任务链超时: {job_id}")


# ---------------------------------------------------------------- 完整离线闭环


@pytest.mark.api
def test_manual_pipeline_runs_full_offline_chain(client):
    """手动触发跑完整条链,五个步骤都要有明确结论。"""
    response = _post_pipeline(client, trade_date=AS_OF)

    assert response.status_code == 202
    assert response.json()["reused"] is False
    payload = _wait_for_pipeline(client, response.json()["job_id"])

    assert payload["status"] == "succeeded", payload.get("error")
    steps = {step["name"]: step for step in payload["result"]["steps"]}
    assert set(steps) == {
        "ingest_market",
        "scan",
        "backfill_returns",
        "collect_news",
        "postmortem",
    }
    # 每一步都必须落一个三态结论,不允许出现空状态
    assert all(step["status"] in {"ok", "skipped", "unavailable"} for step in steps.values())
    assert steps["scan"]["status"] == "ok"
    assert steps["scan"]["data"]["final_count"] > 0


@pytest.mark.api
def test_news_step_reports_unavailable_not_fake_success(client):
    """未配置舆情来源:必须明确标记 unavailable,不能假装成功。

    这条是"不静默降级"的落点——一个没跑的步骤如果报 ok,
    复盘结果里"今日无重要舆情"就成了编造。

    仓库默认配置 news.enabled=false,因此这里期望 reason=news_disabled;
    若将来默认开启但没有启用来源,reason 会是 no_enabled_source,
    两者都属于"确实没执行",与"执行了但采到 0 条"必须区分。
    """
    job_id = _post_pipeline(client, trade_date=AS_OF).json()["job_id"]
    payload = _wait_for_pipeline(client, job_id)

    news = next(s for s in payload["result"]["steps"] if s["name"] == "collect_news")
    assert news["status"] == "unavailable"
    assert news["data"]["reason"] in {"news_disabled", "no_enabled_source"}
    assert news["data"]["collected"] == 0
    assert "collect_news" in payload["result"]["unavailable_steps"]


# ---------------------------------------------------------------- 幂等


@pytest.mark.api
def test_repeat_pipeline_reuses_completed_batch(client):
    """同一交易日同策略重复触发:不重复写入,返回既有批次。

    用 200 而不是 202:根本没有新任务被排队,202 会让前端一直轮询
    一个不会再变的任务。
    """
    first = _post_pipeline(client, trade_date=AS_OF)
    assert first.status_code == 202
    first_id = first.json()["job_id"]
    _wait_for_pipeline(client, first_id)

    second = _post_pipeline(client, trade_date=AS_OF)

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["job_id"] == first_id


@pytest.mark.api
def test_force_rerun_creates_new_pipeline(client):
    first_id = _post_pipeline(client, trade_date=AS_OF).json()["job_id"]
    _wait_for_pipeline(client, first_id)

    second = _post_pipeline(client, trade_date=AS_OF, force=True)

    assert second.status_code == 202
    assert second.json()["job_id"] != first_id
    assert _wait_for_pipeline(client, second.json()["job_id"])["status"] == "succeeded"


@pytest.mark.api
def test_pipeline_trade_date_matches_written_batch(client):
    """任务的 trade_date 必须等于扫描实际写入的 as_of。

    抢占时用的是闸门预解析的日期;在线模式可能抓到更新的交易日。
    不回写真实 as_of,幂等键就会指向一个并不存在的批次,重跑拦不住。
    """
    job_id = _post_pipeline(client, trade_date=AS_OF).json()["job_id"]
    payload = _wait_for_pipeline(client, job_id)

    scan = next(s for s in payload["result"]["steps"] if s["name"] == "scan")
    assert payload["trade_date"] == scan["data"]["as_of"]


# ---------------------------------------------------------------- 交易日校验


@pytest.mark.api
def test_non_trading_day_is_rejected(client):
    """指定的日期不是开市日 -> 400,绝不为它生成批次。"""
    response = _post_pipeline(client, trade_date="20250105")  # 周日

    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"not_trading_day", "calendar_missing"}


@pytest.mark.api
def test_malformed_trade_date_is_rejected(client):
    response = _post_pipeline(client, trade_date="2025-13-99")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_trade_date"


@pytest.mark.api
def test_auto_trigger_rejected_when_calendar_stale(client):
    """日历没覆盖到今天 -> 拒绝自动触发,并说明原因。

    固定装置的日历停在 2025 年,所以这条走的就是真实的 `calendar_stale` 分支。
    拒绝时必须给出可展示的理由,而不是静默不跑。
    """
    response = _post_pipeline(client)

    assert response.status_code in {409, 503}
    error = response.json()["error"]
    assert error["code"] in {"pipeline_not_due", "calendar_unusable"}
    assert error["message"]


# ---------------------------------------------------------------- 状态查询


@pytest.mark.api
def test_schedule_status_reports_config_and_gate(client):
    response = client.get("/api/pipelines/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_after"]
    assert payload["strategy"]
    assert payload["gate"]["should_run"] is False
    assert payload["gate"]["reason"] == "calendar_stale"
    # enabled 与 running 必须分开上报:关掉配置和线程崩了是两回事
    assert "enabled" in payload and "running" in payload


@pytest.mark.api
def test_status_route_not_shadowed_by_job_id(client):
    """/pipelines/status 不能被 /pipelines/{job_id} 吃掉。"""
    assert client.get("/api/pipelines/status").status_code == 200
    assert client.get("/api/pipelines/not-a-real-job").status_code == 404


@pytest.mark.api
def test_unknown_pipeline_returns_not_found(client):
    response = client.get("/api/pipelines/not-found")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "pipeline_job_not_found"


@pytest.mark.api
def test_list_recent_pipelines(client):
    job_id = _post_pipeline(client, trade_date=AS_OF).json()["job_id"]
    _wait_for_pipeline(client, job_id)

    response = client.get("/api/pipelines", params={"limit": 5})

    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert items[0]["job_id"] == job_id
    assert items[0]["kind"] == "close_pipeline"


@pytest.mark.api
def test_invalid_limit_rejected(client):
    response = client.get("/api/pipelines", params={"limit": 0})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_limit"
