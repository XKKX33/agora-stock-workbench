"""扫描任务 API：离线执行、幂等重跑、强制重跑与状态查询。

幂等相关用例覆盖的是"同一交易日同策略只跑一批"这条业务规则，
而不是 HTTP 层的重试语义——键是 (kind, trade_date, strategy)。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from engine.db import Store
from tests.api.conftest import wait_for_job
from tests.test_run_scan_offline import AS_OF, _TRADE_DATES

# 固定装置的 160 个日历日全部开市:基准日 = AS_OF,可见日 = 往前退 20 个开市日。
VISIBLE_AS_OF = _TRADE_DATES[-21]


def _post_scan(client, **overrides):
    body = {"strategy": "strong_mainup", "online": False, "record": True}
    body.update(overrides)
    return client.post("/api/scans", json=body)


def test_scan_job_runs_offline(client):
    response = _post_scan(client)

    assert response.status_code == 202
    assert response.json()["reused"] is False
    payload = wait_for_job(client, response.json()["job_id"])
    assert payload["status"] == "succeeded"
    assert payload["result"]["scored_count"] > 0
    progress = payload["result"]["progress"]
    assert progress["stage"] == "complete"
    assert progress["percent"] == 100
    assert payload["result"]["steps"]
    assert payload["result"]["steps"][-1]["name"] == "score"
    assert payload["result"]["steps"][-1]["status"] == "succeeded"
    assert payload["result"]["progress"]["logs"]


def test_unknown_scan_job_returns_not_found(client):
    response = client.get("/api/scans/not-found")

    assert response.status_code == 404


def test_repeat_scan_reuses_completed_job(client):
    """同一交易日同策略重复提交：不新建任务，返回既有结果。

    用 200 而不是 202：202 表示"已受理、稍后完成"，会让前端一直轮询一个
    不会再变的任务；这里根本没有新任务被排队。
    """
    first = _post_scan(client)
    assert first.status_code == 202
    first_id = first.json()["job_id"]
    wait_for_job(client, first_id)

    second = _post_scan(client)

    assert second.status_code == 200
    body = second.json()
    assert body["reused"] is True
    assert body["job_id"] == first_id
    assert body["status"] == "succeeded"


def test_force_rerun_creates_new_job(client):
    """force=True 绕过幂等拦截，产生新任务。"""
    first = _post_scan(client)
    first_id = first.json()["job_id"]
    wait_for_job(client, first_id)

    second = _post_scan(client, force=True)

    assert second.status_code == 202
    assert second.json()["reused"] is False
    assert second.json()["job_id"] != first_id
    assert wait_for_job(client, second.json()["job_id"])["status"] == "succeeded"


def test_scan_survives_new_app_instance(client, db_path):
    """任务状态在库里而不是进程内存里：换一个 app 实例仍能查到。

    旧实现用内存 dict 存任务，服务重启后历史任务全部查不到，
    也就无法跨进程阻止同一批次被重复写入。
    """
    job_id = _post_scan(client).json()["job_id"]
    wait_for_job(client, job_id)

    settings = AppSettings(
        workbench_root=Path(__file__).resolve().parents[2],
        database_path=db_path,
    )
    with TestClient(create_app(settings)) as fresh:
        response = fresh.get(f"/api/scans/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_list_recent_scans(client):
    job_id = _post_scan(client).json()["job_id"]
    wait_for_job(client, job_id)

    response = client.get("/api/scans", params={"limit": 5})

    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert items[0]["job_id"] == job_id
    assert items[0]["kind"] == "scan"


def test_trade_date_matches_recorded_batch(client):
    """任务的 trade_date 必须等于实际写入的 as_of。

    抢占时用的是本地预解析日期；若完成后不回写真实 as_of，
    幂等键会指向一个并不存在的批次，下一次重跑就拦不住。
    """
    job_id = _post_scan(client).json()["job_id"]
    payload = wait_for_job(client, job_id)

    assert payload["trade_date"] == payload["result"]["as_of"]



def test_scan_targets_the_visible_session_not_the_latest(client):
    """扫描截面必须是可见日:隐藏窗口里的行情不许拿来选股。

    本地库确实有比可见日更新的行情(AS_OF),旧实现直接用它当截面,
    等于拿"当时还看不到的数据"选股,是最直接的前视偏差。
    """
    payload = wait_for_job(client, _post_scan(client).json()["job_id"])

    assert payload["status"] == "succeeded"
    assert payload["trade_date"] == VISIBLE_AS_OF
    assert payload["result"]["as_of"] == VISIBLE_AS_OF
    assert VISIBLE_AS_OF < AS_OF


def test_scan_rejects_when_visible_window_unavailable(tmp_path: Path):
    """算不出可见日(库里没有基准日)时 409,绝不回退成最新交易日。"""
    db_path = tmp_path / "no-window.duckdb"
    with Store(db_path):
        pass

    settings = AppSettings(
        workbench_root=Path(__file__).resolve().parents[2],
        database_path=db_path,
    )
    with TestClient(create_app(settings)) as fresh:
        response = _post_scan(fresh)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "visibility_window_unavailable"