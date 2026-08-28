"""策略名在入口就要校验。

拼错的策略名以前会先起一个后台任务,跑到读配置文件那一步才 FileNotFoundError,
报错里带着服务器绝对路径。用户想知道的只是"名字写错了,能选哪些"。

运行:
    cd workbench
    python -m pytest tests/api/test_strategy_validation.py -q
"""

from __future__ import annotations

import json

import pytest

from engine.config import StrategyNotFound, available_strategies

pytestmark = pytest.mark.api


def test_available_strategies_lists_registered_files():
    names = available_strategies()

    assert names, "config/strategies 下没有任何策略,后面的测试失去意义"
    assert names == sorted(names)
    assert "strong_mainup" in names


def test_load_strategy_names_the_options_instead_of_a_file_path():
    from engine.config import load_strategy

    with pytest.raises(StrategyNotFound) as excinfo:
        load_strategy("nosuchstrategy")

    message = str(excinfo.value)
    assert "nosuchstrategy" in message
    assert "strong_mainup" in message, "报错没告诉用户能选什么"
    # 报错不能夹带服务器路径
    assert "config" not in message and ".yaml" not in message


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/scans", {"strategy": "nosuchstrategy"}),
        ("/api/pipelines", {"strategy": "nosuchstrategy"}),
        ("/api/pipelines/backfill", {"strategy": "nosuchstrategy", "count": 1}),
    ],
)
def test_unknown_strategy_is_rejected_before_any_job_starts(client, path, body):
    """422 在入口拦下,不留下任何任务行。"""
    before = len(client.get("/api/scans").json()["items"])

    response = client.post(path, json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "request_validation_failed"
    detail = json.dumps(error, ensure_ascii=False)
    assert "strong_mainup" in detail, "报错没列出可选策略"

    after = len(client.get("/api/scans").json()["items"])
    assert after == before, "非法策略仍然起了任务"


def test_omitted_strategy_still_means_use_the_configured_default():
    """None 表示沿用配置默认值,不该被校验器拒掉。

    刻意只测 schema:POST /api/pipelines 会真的起一条一键链(联网摸行情、抓热榜),
    为了验"校验器不误伤 None"付出那个代价不值得,而且测试会因为外部服务而不稳。
    """
    from app.schemas.pipelines import PipelineBackfillRequest, PipelineRequest
    from app.schemas.scans import ScanRequest

    assert PipelineRequest().strategy is None
    assert PipelineRequest(strategy="strong_mainup").strategy == "strong_mainup"
    assert PipelineBackfillRequest().strategy is None
    # 扫描请求的 strategy 不可为空,默认值必须是真实登记过的策略
    assert ScanRequest().strategy in available_strategies()


@pytest.mark.parametrize(
    "model_path,field",
    [
        ("app.schemas.scans:ScanRequest", "strategy"),
        ("app.schemas.pipelines:PipelineRequest", "strategy"),
        ("app.schemas.pipelines:PipelineBackfillRequest", "strategy"),
    ],
)
def test_schema_rejects_unknown_strategy(model_path, field):
    """三个请求模型共用同一个校验器,任何一个漏接都会在这里红。"""
    import importlib

    module_name, class_name = model_path.split(":")
    model = getattr(importlib.import_module(module_name), class_name)

    with pytest.raises(ValueError) as excinfo:
        model(**{field: "nosuchstrategy"})

    assert "nosuchstrategy" in str(excinfo.value)
