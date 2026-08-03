"""AI 接口边界的 API 测试。

夹具环境没有配置 AI,因此这里锁的是"未配置时接口如实报告、生成接口
返回 503 而不是编内容"。

运行:
    cd workbench
    python -m pytest tests/api/test_ai.py -q
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


def test_ai_status_reports_not_configured(client):
    response = client.get("/api/ai/status")

    assert response.status_code == 200
    payload = response.json()
    # settings.yaml 里 ai.enabled 为 false,应报 disabled;
    # 若有人把它打开了但没配凭据,则应报 unconfigured。两者都不是 available。
    assert payload["availability"] in {"disabled", "unconfigured"}
    assert payload["availability"] != "available"
    assert payload["reason"]


def test_ai_review_fails_loudly_when_unconfigured(client):
    """未配置时返回 503,而不是一段看起来像 AI 结论的文本。"""
    response = client.post("/api/ai/reviews")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "ai_unavailable"
    assert error["message"]
    # details 里带上可用性自述,页面不必再发一次 status 请求
    assert error["details"]["availability"] != "available"


def test_ai_review_body_has_no_fabricated_narrative(client):
    """失败响应里不能夹带任何 narrative 字段。"""
    payload = client.post("/api/ai/reviews").json()

    assert "narrative" not in payload
