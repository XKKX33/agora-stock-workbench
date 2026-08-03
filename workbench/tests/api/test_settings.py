"""设置接口测试:读写 settings.local.yaml(用临时目录隔离)。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.api

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
import app.services.settings_store as ss
import engine.config as engine_config


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # 把引擎配置目录与设置存储都指到临时目录,确保不污染真实 config。
    fake_dir = tmp_path / "config"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / "settings.yaml").write_text(
        "agent:\n  enabled: false\n  provider: openai_compatible\n", encoding="utf-8"
    )
    monkeypatch.setattr(engine_config, "CONFIG_DIR", fake_dir)
    monkeypatch.setattr(ss, "CONFIG_DIR", fake_dir)
    monkeypatch.setattr(ss, "LOCAL_FILE", fake_dir / "settings.local.yaml")
    settings = AppSettings(
        workbench_root=Path(__file__).resolve().parents[2],
        database_path=tmp_path / "market.duckdb",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_settings_read_returns_defaults(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert data["agent"]["provider"] == "openai_compatible"
    assert data["agent"]["default_candidates"] > 0
    assert data["api_key_hint"]


def test_settings_put_and_read(client):
    r = client.put("/api/settings", json={
        "agent": {
            "model": "deepseek-chat",
            "base_url": "http://127.0.0.1:9999/v1",
            "api_key_env": "TEST_KEY",
            "default_candidates": 120,
            "default_depth": 6,
            "default_final": 2,
        }
    })
    assert r.status_code == 200
    assert r.json()["saved"]["agent"]["model"] == "deepseek-chat"

    r2 = client.get("/api/settings")
    data = r2.json()
    assert data["agent"]["model"] == "deepseek-chat"
    assert data["agent"]["default_candidates"] == 120
