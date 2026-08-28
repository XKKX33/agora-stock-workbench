from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from engine.db import Store
from engine.run_scan import run_scan
from engine.visibility import DEFAULT_DELAY_SESSIONS
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
import sys

import engine.config as engine_config
import engine.ml.registry as ml_registry


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
    """API 测试必须离线:统一把舆情采集关掉、AI 凭据清掉,不依赖仓库默认配置。

    仓库默认 settings.yaml 已开启 news.enabled=true(一键采集是产品功能),
    测试不能假设默认是关的——否则手动触发盘后链时会真的去抓全网热榜。
    这里显式隔离:其余配置保持真实值,只把 news 段改成"未启用、无来源"。

    凭据同理。ai / agent 两段都从 `api_key_env` 指定的环境变量读密钥,开发机
    上一旦导出过这个变量,"没配凭据要报 unconfigured"的用例就会翻红,更糟的
    是 AI 复盘用例会真的去打模型接口。所以按配置里声明的变量名逐个删掉——
    需要配好凭据的用例自己 setenv,夹具先跑,不会被覆盖。

    隐藏窗口(visibility_delay_sessions)也必须钉死在代码默认值上。它是**运营
    可调参数**:舆情源只能采实时数据,生产上可能把它调成 0 让选股截面与舆情
    对齐。可这批用例的存在意义就是验证"隐藏窗口内的日期必须被拒"——delay 为 0
    时窗口是空的,断言无事可验、静默空转,闸门坏了也发现不了。需要别的 delay
    的用例自己覆盖这个键。
    """
    isolated = copy.deepcopy(engine_config.load_settings())
    news = isolated.setdefault("news", {})
    news["enabled"] = False
    news["sources"] = []
    data = isolated.setdefault("data", {})
    data["visibility_delay_sessions"] = DEFAULT_DELAY_SESSIONS
    # 先抓原函数引用:下面要靠身份比较认出"从 engine.config 导入过来的同一个
    # 函数",一旦 engine_config 上的名字先被换掉,比较就全部落空。
    originals = {
        attr: getattr(engine_config, attr)
        for attr in ("load_settings", "load_settings_with_local")
    }
    # 同时隔离"本地覆盖 settings.local.yaml",避免开发机上的 UI 设置污染测试
    monkeypatch.setattr(engine_config, "load_settings", lambda: isolated)
    monkeypatch.setattr(engine_config, "load_settings_with_local", lambda: isolated)
    for section in ("ai", "agent"):
        env_name = str(
            (isolated.get(section) or {}).get("api_key_env") or ""
        ).strip() or "WORKBENCH_AI_API_KEY"
        monkeypatch.delenv(env_name, raising=False)
    # 模块清单不手写:`from engine.config import load_settings` 会在导入方模块里
    # 复制一份名字绑定,补 engine.config 上的原名对它无效。手写清单漏一个模块就
    # 静默读回真实配置——之前 app.services.returns / reviews 就是这么漏掉的。
    # 测试模块自己也算:有用例调 load_settings() 算期望值,读的必须是同一份隔离
    # 配置,否则期望值按真配置算、接口按隔离配置跑,断言比的是两套口径。
    # 设置读写模块是"被测对象"而不是配置消费者:/api/settings 的用例要写进临时
    # 目录再读回来验证往返。冻成静态字典的话它永远读不到自己刚写的值。它自己
    # 已经把 CONFIG_DIR / LOCAL_FILE 指到 tmp,不需要这里再隔离。
    for name, module in list(sys.modules.items()):
        if module is None or name in ("engine.config", "app.services.settings_store"):
            continue
        if not name.startswith(("app.", "engine.", "tests.")):
            continue
        for attr, original in originals.items():
            if getattr(module, attr, None) is original:
                monkeypatch.setattr(module, attr, lambda: isolated)
    return isolated