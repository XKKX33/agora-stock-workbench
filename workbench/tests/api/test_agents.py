"""多 agent 研判 API 测试。

夹具环境使用默认 agent.enabled=true（且无凭据），因此锁的是
"未配置时如实报告、发起研判返回 503 而不是编结果"。

运行:
    cd workbench
    python -m pytest tests/api/test_agents.py -q
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.api


def test_agents_status_reports_not_available(client):
    response = client.get("/api/agents/status")

    assert response.status_code == 200
    payload = response.json()
    # 默认已启用但没有凭据，必须明确报告 unconfigured。
    assert payload["availability"] == "unconfigured"
    assert payload["availability"] != "available"
    assert payload["agent_enabled"] is True
    assert payload["defaults"]["candidates"] > 0
    assert payload["limits"]["max_candidates"] >= payload["defaults"]["candidates"]


def test_agents_judge_fails_loudly_when_unconfigured(client):
    """缺少凭据时返回 503，不能假装开始跑。"""
    response = client.post(
        "/api/agents/judge",
        json={"candidates": 10, "depth": 4, "final": 2},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "agent_unconfigured"
    assert error["message"]
    assert error["details"]["availability"] != "available"


def test_agents_jobs_empty_list(client):
    response = client.get("/api/agents/jobs")

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_agents_job_not_found(client):
    response = client.get("/api/agents/jobs/00000000000000000000000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_judge_job_not_found"


def test_agents_results_empty_when_no_runs(client):
    response = client.get("/api/agents/results")

    assert response.status_code == 200
    payload = response.json()
    assert payload["as_of"] is None
    assert payload["items"] == []


def test_agents_results_filters_by_as_of_and_status(db_path, client):
    import pandas as pd

    from engine.db import Store

    base = {
        "stage": "done",
        "candidates": 200,
        "depth": 8,
        "final_count": 3,
        "progress_json": "{}",
        "created_at": "2026-07-30T00:00:00+00:00",
        "started_at": None,
        "finished_at": "2026-07-30T00:01:00+00:00",
        "heartbeat_at": "2026-07-30T00:01:00+00:00",
        "error_json": None,
    }
    rows = [
        {
            **base,
            "run_id": "r-old",
            "as_of": "20260730",
            "status": "succeeded",
            "result_json": json.dumps(
                {"as_of": "20260730", "final": []}, ensure_ascii=False
            ),
        },
        {
            **base,
            "run_id": "r-new",
            "as_of": "20260802",
            "status": "succeeded",
            "created_at": "2026-08-02T00:00:00+00:00",
            "finished_at": "2026-08-02T00:01:00+00:00",
            "result_json": json.dumps(
                {
                    "as_of": "20260802",
                    "final": [
                        {
                            "ts_code": "000001.SZ",
                            "name": "平安银行",
                            "score": 82.5,
                            "verdict": "看多",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            **base,
            "run_id": "r-failed",
            "as_of": "20260802",
            "status": "failed",
            "created_at": "2026-08-02T00:02:00+00:00",
            "error_json": json.dumps(
                {"type": "AgentOutputError", "message": "模型返回空内容"},
                ensure_ascii=False,
            ),
        },
    ]
    with Store(db_path, ensure_schema=True) as store:
        for row in rows:
            store.record_agent_run(row)
        store.upsert_agent_judgments(
            pd.DataFrame(
                [
                    {
                        "run_id": "r-new",
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "industry": "银行",
                        "rank": 1,
                        "score": 82.5,
                        "stance": "bullish",
                        "thesis": "情绪启动+资金确认",
                        "risks": json.dumps(["大盘回调"], ensure_ascii=False),
                        "stage_json": json.dumps(
                            {"verdict": "看多", "action": "回踩低吸"},
                            ensure_ascii=False,
                        ),
                    }
                ]
            )
        )

    # 默认:只含 succeeded,按创建时间倒序(r-new 在前)
    response = client.get("/api/agents/results")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["run_id"] for item in items] == ["r-new", "r-old"]
    assert all(item["status"] == "succeeded" for item in items)

    newest = items[0]
    assert newest["as_of"] == "20260802"
    assert newest["params"] == {"candidates": 200, "depth": 8, "final": 3}
    assert newest["summary"]["final"][0]["ts_code"] == "000001.SZ"
    assert newest["judgments"][0]["thesis"] == "情绪启动+资金确认"
    assert newest["judgments"][0]["risks"] == ["大盘回调"]
    assert newest["judgments"][0]["stage"]["action"] == "回踩低吸"

    # as_of 过滤
    response = client.get("/api/agents/results", params={"as_of": "20260802"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["run_id"] for item in items] == ["r-new"]
    assert response.json()["as_of"] == "20260802"

    # 不存在的 as_of
    response = client.get("/api/agents/results", params={"as_of": "19990101"})
    assert response.status_code == 200
    assert response.json()["items"] == []

    # limit 生效(只返回最新一条)
    response = client.get("/api/agents/results", params={"limit": 1})
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["run_id"] for item in items] == ["r-new"]

    # 非法 limit
    response = client.get("/api/agents/results", params={"limit": 0})
    assert response.status_code == 422


def test_agents_single_fails_loudly_when_disabled(client):
    """单只研判同样未配置时返回 503,不允许假装跑。"""
    response = client.post(
        "/api/agents/single",
        json={"ts_code": "000001.SZ"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] in {"agent_disabled", "agent_unconfigured"}
    assert error["details"]["availability"] != "available"
