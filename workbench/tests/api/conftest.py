from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from engine.db import Store
from engine.run_scan import run_scan
from tests.test_run_scan_offline import _seed_db


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "market.duckdb"
    with Store(path) as store:
        _seed_db(store)
    run_scan(
        strategy_name="strong_mainup",
        online=False,
        db_path=str(path),
        record=True,
    )
    return path


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    settings = AppSettings(
        workbench_root=Path(__file__).resolve().parents[2],
        database_path=db_path,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/scans/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"扫描任务超时: {job_id}")



import copy

import app.services.agents as agents_mod
import app.services.ai as ai_service_mod
import app.services.news_collect as news_collect_mod
import app.services.pipelines as pipelines_mod
import app.services.scans as scans_mod
import engine.close_pipeline as close_pipeline_mod
import engine.config as engine_config
import engine.ml.registry as ml_registry
import engine.run_scan as run_scan_mod


@pytest.fixture(autouse=True)
def model_dir(tmp_path: Path, monkeypatch) -> Path:
    """产物目录指向空的临时目录。

    registry.DEFAULT_DIR 指向仓库里的 data/models,那里有没有文件取决于
    开发机上跑过没跑过训练。不隔离的话,同一份接口测试在训练前后会得到
    not_trained 和 pending 两种结果——测试就变成了在测环境,不是测代码。
    需要产物的用例自己往这个目录写。
    """
    directory = tmp_path / "models"
    directory.mkdir()
    monkeypatch.setattr(ml_registry, "DEFAULT_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    """API 测试必须离线:统一把舆情采集关掉,不依赖仓库默认配置。

    仓库默认 settings.yaml 已开启 news.enabled=true(一键采集是产品功能),
    测试不能假设默认是关的——否则手动触发盘后链时会真的去抓全网热榜。
    这里显式隔离:其余配置保持真实值,只把 news 段改成"未启用、无来源"。
    """
    isolated = copy.deepcopy(engine_config.load_settings())
    news = isolated.setdefault("news", {})
    news["enabled"] = False
    news["sources"] = []
    # 同时隔离"本地覆盖 settings.local.yaml",避免开发机上的 UI 设置污染测试
    monkeypatch.setattr(engine_config, "load_settings", lambda: isolated)
    monkeypatch.setattr(engine_config, "load_settings_with_local", lambda: isolated)
    for module in (
        close_pipeline_mod,
        run_scan_mod,
        news_collect_mod,
        pipelines_mod,
        scans_mod,
        ai_service_mod,
        agents_mod,
    ):
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda: isolated)
        if hasattr(module, "load_settings_with_local"):
            monkeypatch.setattr(module, "load_settings_with_local", lambda: isolated)
    return isolated