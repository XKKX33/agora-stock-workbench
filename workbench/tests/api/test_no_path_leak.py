"""对外响应不许泄露服务器磁盘布局。

任务失败信息、配置读回都可能夹带 `C:\\Users\\<用户名>\\...`。这些响应直接渲染
到页面上,任何能打开页面的人都会看到服务器的目录结构,而这对排障毫无帮助——
排障看的是服务端日志,那里保留原始路径。

运行:
    cd workbench
    python -m pytest tests/api/test_no_path_leak.py -q
"""

from __future__ import annotations

import json

import pytest

from app.services.tasks import TaskTracker

pytestmark = pytest.mark.api

# 出现任何一个就说明磁盘布局漏了出去
PATH_MARKERS = ["C:\\Users", "C:/Users", "/home/", "/Users/", "workbench\\", "workbench/"]


def _assert_clean(payload: object, where: str) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for marker in PATH_MARKERS:
        assert marker not in blob, f"{where} 的响应里带着服务器路径片段 {marker!r}"


def test_task_error_is_redacted_before_leaving_the_tracker():
    """TaskTracker.decorate 是任务错误流向浏览器的唯一出口。"""
    row = {
        "task_id": "t1",
        "kind": "scan",
        "status": "failed",
        "error_json": json.dumps(
            {
                "type": "FileNotFoundError",
                "message": (
                    "[Errno 2] No such file or directory: "
                    "'C:\\\\Users\\\\someone\\\\workbench\\\\config\\\\x.yaml'"
                ),
            }
        ),
        "result_json": None,
    }

    out = TaskTracker.decorate(row)

    assert out["error"]["type"] == "FileNotFoundError"
    assert "[PATH]" in out["error"]["message"]
    _assert_clean(out, "TaskTracker.decorate")


def test_decorate_drops_the_raw_json_columns():
    """原始列必须丢掉。

    留着 error_json 等于把刚脱敏掉的原文又原样带出去一份——脱敏只做一半
    比不做更危险,因为看起来是干净的。
    """
    row = {
        "task_id": "t2",
        "status": "failed",
        "error_json": json.dumps({"message": "boom at C:\\\\Users\\\\me\\\\x.yaml"}),
        "result_json": json.dumps({"ok": 1}),
    }

    out = TaskTracker.decorate(row)

    assert "error_json" not in out
    assert "result_json" not in out
    assert out["result"] == {"ok": 1}
    _assert_clean(out, "decorate 原始列")


def test_settings_response_does_not_expose_the_config_file_path(client):
    """设置页面不需要知道配置文件在磁盘哪个位置。"""
    payload = client.get("/api/settings").json()

    assert "local_file" not in payload
    _assert_clean(payload, "/api/settings")


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/overview",
        "/api/settings",
        "/api/scans",
        "/api/pipelines",
        "/api/agents/jobs",
        "/api/agents/status",
        "/api/news/collect/jobs",
        "/api/pipelines/status",
    ],
)
def test_read_endpoints_never_carry_server_paths(client, path):
    response = client.get(path)

    assert response.status_code == 200
    _assert_clean(response.json(), path)
