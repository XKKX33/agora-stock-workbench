"""实验查询 API 测试：所有数据只写入 ``tmp_path`` 临时数据库。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from engine.db import Store
from engine.returns import HORIZONS


pytestmark = pytest.mark.api

GROUPS = ("rule", "ai", "hybrid", "benchmark")


def _run_row(run_id: str, as_of: str) -> dict:
    return {
        "run_id": run_id,
        "as_of": as_of,
        "data_cutoff_at": f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T15:30:00+08:00",
        "status": "running",
        "strategy_name": "hermes",
        "strategy_version": "v1",
        "model": "deepseekv4flash",
        "temperature": 0.1,
        "prompt_version": "p1",
        "candidate_hash": f"sha256:{run_id}",
        "candidate_count": 1,
        "final_count": 1,
        "hybrid_rule_weight": 0.5,
        "hybrid_ai_weight": 0.5,
        "created_at": f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T15:31:00+08:00",
        "finished_at": f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T15:32:00+08:00",
        "error_json": None,
    }


CODES = {
    "rule": "000002.SZ",
    "ai": "000001.SZ",
    "hybrid": "000003.SZ",
    "benchmark": "000004.SZ",
}


def _decisions(run_id: str) -> pd.DataFrame:
    """四组决策明细:只存决策本身,成交与收益一律落 experiment_returns。"""
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "group_name": group_name,
                "ts_code": CODES[group_name],
                "name": f"{group_name}样本",
                "industry": "测试行业",
                "rank": 1,
                "rule_score": 80.0,
                "ai_score": 70.0,
                "hybrid_score": 75.0,
                "reason_json": None,
                "risk_json": None,
            }
            for group_name in GROUPS
        ]
    )


def _sessions(as_of: str) -> list[str]:
    """信号日之后的十个交易日;接口层只关心日期序列,用工作日近似即可。"""
    start = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    return [day.strftime("%Y%m%d") for day in pd.bdate_range(start=start, periods=10)]


def _return_rows(
    run_id: str,
    group_name: str,
    *,
    as_of: str,
    entry_price: float | None,
    status: str,
    reason: str | None,
    measured: dict[str, float] | None = None,
) -> list[dict]:
    """按 experiment_returns 的口径造一条决策的十行收益明细。

    ``measured`` 里的期限记为已成交并带真实收益;其余期限只留 status/reason,
    算不出的格子绝不用 0 冒充。
    """
    measured = measured or {}
    sessions = _sessions(as_of)
    rows = []
    for index, horizon in enumerate(HORIZONS):
        filled = horizon in measured
        gross_return = measured.get(horizon)
        rows.append(
            {
                "run_id": run_id,
                "group_name": group_name,
                "ts_code": CODES[group_name],
                "horizon": horizon,
                "entry_date": sessions[0],
                "entry_price": entry_price,
                "sell_date": sessions[index],
                "sell_session": "close" if index == 0 else "open",
                "sell_price": (
                    round(entry_price * (1.0 + gross_return), 6) if filled else None
                ),
                "status": "filled" if filled else status,
                "reason": None if filled else reason,
                "gross_return": gross_return,
                "created_at": "2026-08-05T09:00:00+08:00",
                "updated_at": "2026-08-05T09:00:00+08:00",
            }
        )
    return rows


@pytest.fixture()
def experiment_client(client, db_path):
    with Store(db_path, ensure_schema=False) as store:
        store.record_experiment(
            _run_row("run-20260803", "20260803"),
            _decisions("run-20260803"),
        )
        store.record_experiment(
            _run_row("run-20260804", "20260804"),
            _decisions("run-20260804"),
        )
        store.upsert_experiment_returns(
            [
                # 老批次缺涨跌停价:算过收益,但成交结果还没定下来。
                *[
                    row
                    for group_name in GROUPS
                    for row in _return_rows(
                        "run-20260803",
                        group_name,
                        as_of="20260803",
                        entry_price=None,
                        status="pending_entry",
                        reason="limit_price_missing",
                    )
                ],
                # AI 组买到了,T+1 收盘真实收益恰好为 0,不能在序列化里变成空值。
                *_return_rows(
                    "run-20260804",
                    "ai",
                    as_of="20260804",
                    entry_price=10.0,
                    status="future_not_reached",
                    reason=None,
                    measured={"t1_close": 0.0, "t3_open": 0.2},
                ),
                # 混合组买到了,T+1 收盘是负收益。
                *_return_rows(
                    "run-20260804",
                    "hybrid",
                    as_of="20260804",
                    entry_price=10.0,
                    status="future_not_reached",
                    reason=None,
                    measured={"t1_close": -0.1},
                ),
                # 规则组涨停封板:买不到,买入价必须留空。
                *_return_rows(
                    "run-20260804",
                    "rule",
                    as_of="20260804",
                    entry_price=None,
                    status="entry_unavailable",
                    reason="limit_up_locked",
                ),
                # 基准组从没算过收益:experiment_returns 里一行都没有。
            ]
        )
    return client


def test_experiments_filter_by_signal_date_group_stock_and_entry_status(
    experiment_client,
):
    response = experiment_client.get(
        "/api/experiments",
        params={
            "as_of": "20260804",
            "group": "ai",
            "ts_code": "000001.SZ",
            "entry_status": "filled",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["as_of"] == "20260804"
    assert item["group_name"] == "ai"
    assert item["ts_code"] == "000001.SZ"
    assert item["entry_status"] == "filled"
    assert item["entry_date"] == "20260805"
    assert item["entry_price"] == 10.0
    # 真实收益恰好为 0:序列化后必须还是 0.0,不能被当成"没有收益"。
    assert item["returns"]["t1_close"] == {
        "gross_return": 0.0,
        "status": "filled",
        "reason": None,
        "sell_date": "20260805",
        "sell_session": "close",
        "sell_price": 10.0,
    }
    assert item["returns"]["t3_open"]["gross_return"] == 0.2
    # 还没到的期限只留 status,不用 0 冒充。
    assert item["returns"]["t5_open"]["gross_return"] is None
    assert item["returns"]["t5_open"]["status"] == "future_not_reached"
    assert set(item["returns"]) == set(HORIZONS)


def test_experiments_expose_negative_returns_as_measured(experiment_client):
    items = experiment_client.get(
        "/api/experiments",
        params={"as_of": "20260804", "group": "hybrid"},
    ).json()["items"]

    assert [item["ts_code"] for item in items] == ["000003.SZ"]
    assert items[0]["entry_status"] == "filled"
    assert items[0]["returns"]["t1_close"]["gross_return"] == -0.1
    assert items[0]["returns"]["t1_close"]["status"] == "filled"


def test_experiments_keep_decisions_without_any_return_rows_empty(experiment_client):
    """从没算过收益的决策:成交状态留空,不能被伪装成"买不到"或 0 收益。"""
    items = experiment_client.get(
        "/api/experiments",
        params={"as_of": "20260804", "group": "benchmark"},
    ).json()["items"]

    assert [item["ts_code"] for item in items] == ["000004.SZ"]
    item = items[0]
    assert item["entry_status"] is None
    assert item["entry_date"] is None
    assert item["entry_price"] is None
    assert item["returns"] == {}


def test_experiments_entry_status_filter_follows_the_return_rows(experiment_client):
    def selected(entry_status: str) -> list[tuple[str, str]]:
        payload = experiment_client.get(
            "/api/experiments", params={"entry_status": entry_status}
        ).json()
        return sorted((item["as_of"], item["ts_code"]) for item in payload["items"])

    # filled:收益明细里有买入价。
    assert selected("filled") == [
        ("20260804", "000001.SZ"),
        ("20260804", "000003.SZ"),
    ]
    # entry_unavailable:涨停封板或买入日没有 K 线。
    assert selected("entry_unavailable") == [("20260804", "000002.SZ")]
    # pending_entry:算过收益,但既没成交也没判定买不到。
    assert selected("pending_entry") == [
        ("20260803", code) for code in sorted(CODES.values())
    ]
    # 从没算过收益的决策不属于任何一种成交状态。
    never_measured = ("20260804", CODES["benchmark"])
    for entry_status in ("filled", "entry_unavailable", "pending_entry"):
        assert never_measured not in selected(entry_status)


def test_experiments_use_stable_date_group_rank_order(experiment_client):
    items = experiment_client.get("/api/experiments").json()["items"]

    assert [item["as_of"] for item in items] == ["20260804"] * 4 + [
        "20260803"
    ] * 4
    assert [item["group_name"] for item in items[:4]] == [
        "ai",
        "benchmark",
        "hybrid",
        "rule",
    ]


def test_experiments_use_run_id_as_stable_pagination_tiebreaker(
    experiment_client, db_path
):
    with Store(db_path, ensure_schema=False) as store:
        store.record_experiment(
            _run_row("aaa-20260804-rerun", "20260804"),
            _decisions("aaa-20260804-rerun"),
        )

    first = experiment_client.get(
        "/api/experiments",
        params={"as_of": "20260804", "group": "ai", "page": 1, "per_page": 1},
    ).json()["items"]
    second = experiment_client.get(
        "/api/experiments",
        params={"as_of": "20260804", "group": "ai", "page": 2, "per_page": 1},
    ).json()["items"]

    assert [first[0]["run_id"], second[0]["run_id"]] == [
        "aaa-20260804-rerun",
        "run-20260804",
    ]


def test_experiments_filter_by_run_id_isolates_one_batch(experiment_client, db_path):
    """同一信号日跑了两次，run_id 必须只返回选中的那一个批次。

    总览页选定批次后会把 run_id 带进台账页。后端不声明这个参数时 FastAPI 会
    静默丢弃它，用户看到的是当天**全部批次混合**的列表，同一只票出现多次，
    却以为在看自己选的那一批。
    """
    with Store(db_path, ensure_schema=False) as store:
        store.record_experiment(
            _run_row("rerun-20260804", "20260804"),
            _decisions("rerun-20260804"),
        )

    unfiltered = experiment_client.get(
        "/api/experiments", params={"as_of": "20260804", "group": "ai"}
    ).json()
    filtered = experiment_client.get(
        "/api/experiments",
        params={"as_of": "20260804", "group": "ai", "run_id": "rerun-20260804"},
    ).json()

    # 不筛批次：同一只票被两个批次各选一次，台账里就出现两行。
    assert unfiltered["total"] == 2
    assert [item["ts_code"] for item in unfiltered["items"]] == [
        "000001.SZ",
        "000001.SZ",
    ]
    # 筛了批次：只剩选中的那一批，total 也要跟着收窄（分页才不会错）。
    assert filtered["total"] == 1
    assert [item["run_id"] for item in filtered["items"]] == ["rerun-20260804"]


def test_experiments_carry_batch_run_time_so_reruns_are_distinguishable(experiment_client):
    """同一信号日跑多次时，每行必须带批次运行时间，否则台账上分不清哪行属于哪次。

    线上实测 20260821 跑了 3 次、20260706 跑了 4 次。台账只按 as_of 分组，
    同一天的多个批次全挤在一条「信号日」分隔线下，同一只票重复出现且看不出区别——
    用户无法判断自己在看哪一次的入选结果。信号日只说明「基于哪天的行情」，
    说明不了「什么时候跑的这一次」。
    """
    payload = experiment_client.get(
        "/api/experiments", params={"as_of": "20260804", "group": "ai"}
    ).json()

    assert payload["items"], "夹具里应有 ai 组数据"
    for item in payload["items"]:
        assert item.get("run_created_at"), (
            f"{item['run_id']} 缺 run_created_at：台账无法区分同一信号日的多个批次"
        )


def test_experiment_batches_list_every_run_newest_first(experiment_client, db_path):
    """批次列表必须列出全部已落库批次，最新的在前。

    台账的批次下拉框要用它。从分页后的台账数据里提取批次是不行的：一页只有 200 行，
    更早的批次根本不在这一页里，下拉框会缺项，用户选不到自己要看的那一次。
    """
    with Store(db_path, ensure_schema=False) as store:
        store.record_experiment(
            _run_row("later-20260805", "20260805"),
            _decisions("later-20260805"),
        )

    payload = experiment_client.get("/api/experiments/batches").json()
    items = payload["items"]

    run_ids = [item["run_id"] for item in items]
    # 新增的批次必须排在最前，且既有批次一个都不能丢（下拉框缺项等于选不到）。
    assert run_ids[0] == "later-20260805"
    assert {"run-20260804", "run-20260803"} <= set(run_ids)
    # 信号日降序：as_of 是主排序键。
    assert [item["as_of"] for item in items] == sorted(
        (item["as_of"] for item in items), reverse=True
    )
    for item in items:
        for field in ("run_id", "as_of", "created_at", "final_count", "candidate_count"):
            assert item.get(field) is not None, f"{item['run_id']} 缺 {field}"


def test_experiment_batches_exclude_unsucceeded_runs(experiment_client, db_path):
    """没跑成的批次不进下拉框：它没有可看的入选结果，列出来只会让人以为有数据。"""
    with Store(db_path, ensure_schema=False) as store:
        store.create_experiment_run(_run_row("failed-20260806", "20260806"))

    run_ids = [
        item["run_id"]
        for item in experiment_client.get("/api/experiments/batches").json()["items"]
    ]

    assert "failed-20260806" not in run_ids


def test_experiments_unknown_run_id_returns_empty_not_all_rows(experiment_client):
    """筛一个不存在的批次必须返回空，绝不能退化成「忽略条件返回全部」。"""
    payload = experiment_client.get(
        "/api/experiments", params={"run_id": "no-such-run"}
    ).json()

    assert payload["total"] == 0
    assert payload["items"] == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("as_of", "2026-08-04"),
        ("as_of", "20261399"),
        ("group", "unknown"),
        ("ts_code", "000001"),
        ("entry_status", "unknown"),
    ],
)
def test_experiments_reject_invalid_filters(client, name, value):
    response = client.get("/api/experiments", params={name: value})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_experiments_missing_database_uses_error_contract(tmp_path):
    settings = AppSettings(
        workbench_root=Path(__file__).resolve().parents[2],
        database_path=tmp_path / "missing.duckdb",
    )
    app = create_app(settings)

    with TestClient(app, raise_server_exceptions=False) as missing_client:
        response = missing_client.get("/api/experiments")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_experiment_detail_returns_run_and_four_groups(experiment_client):
    response = experiment_client.get("/api/experiments/run-20260804")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["run_id"] == "run-20260804"
    assert payload["run"]["candidate_hash"] == "sha256:run-20260804"
    assert [item["group_name"] for item in payload["items"]] == [
        "ai",
        "benchmark",
        "hybrid",
        "rule",
    ]
    # 详情页和台账列表读同一份收益明细,页面上不会出现第二套口径。
    items = {item["group_name"]: item for item in payload["items"]}
    assert items["ai"]["entry_status"] == "filled"
    assert items["ai"]["returns"]["t1_close"]["gross_return"] == 0.0
    assert items["rule"]["entry_status"] == "entry_unavailable"
    assert items["rule"]["entry_price"] is None
    assert items["rule"]["returns"]["t1_close"]["reason"] == "limit_up_locked"
    assert items["benchmark"]["entry_status"] is None
    assert items["benchmark"]["returns"] == {}


def test_experiment_detail_missing_run_uses_error_contract(client):
    response = client.get("/api/experiments/missing-run")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "experiment_not_found"


def test_experiment_detail_exposes_preflight_failure_without_fake_metadata(
    client, db_path
):
    with Store(db_path, ensure_schema=False) as store:
        store.record_failed_experiment_attempt(
            run_id="preflight-failed",
            as_of="20260804",
            strategy_name="strong_mainup",
            created_at="2026-08-04T15:30:00+08:00",
            finished_at="2026-08-04T15:30:01+08:00",
            error_json='{"type":"AIRequestError"}',
        )

    response = client.get("/api/experiments/preflight-failed")

    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "failed"
    assert run["candidate_count"] is None
    assert run["final_count"] is None


# --- 台账与收益接口同口径 -------------------------------------------------
# `/api/experiments/summary` 已删除,原来由它保护的三条行为迁到
# `/api/returns/summary` 上;这两个接口读同一份 experiment_returns,
# 所以断言写在同一个夹具下,口径漂了立刻红。


def test_returns_summary_preserves_real_zero_and_reports_samples(experiment_client):
    """真实 0 收益必须是 0.0;可测样本数要如实报,不能拿计划数充数。"""
    groups = experiment_client.get(
        "/api/returns/summary", params={"run_id": "run-20260804"}
    ).json()["groups"]

    ai = groups["ai"]["t1_close"]
    assert ai["average"] == 0.0
    assert ai["median"] == 0.0
    assert (ai["measurable_count"], ai["planned_count"]) == (1, 1)
    assert ai["available"] is True
    # T+5 还没到:计划里有这一格,但一个可测样本都没有。
    assert groups["ai"]["t5_open"]["measurable_count"] == 0
    assert groups["ai"]["t5_open"]["average"] is None


def test_returns_summary_never_invents_statistics_without_samples(experiment_client):
    """涨停封板买不到的组:统计量留空,不是 0,也不假装可用。"""
    rule = experiment_client.get(
        "/api/returns/summary", params={"run_id": "run-20260804"}
    ).json()["groups"]["rule"]["t1_close"]

    assert rule["planned_count"] == 1
    assert rule["measurable_count"] == 0
    assert rule["average"] is None
    assert rule["median"] is None
    assert rule["portfolio_gross_return"] is None
    assert rule["available"] is False
    assert rule["status_distribution"] == {"entry_unavailable": 1}


def test_returns_summary_uses_the_same_filters_as_the_ledger(experiment_client):
    """同一组筛选条件下,汇总卡和明细表必须落在同一批决策上。"""
    params = {"as_of": "20260804", "entry_status": "filled"}
    ledger = experiment_client.get("/api/experiments", params=params).json()["items"]
    groups = experiment_client.get("/api/returns/summary", params=params).json()[
        "groups"
    ]

    assert {item["group_name"] for item in ledger} == {"ai", "hybrid"}
    measured = {
        group for group, horizons in groups.items() if horizons["t1_close"]["items"]
    }
    assert measured == {"ai", "hybrid"}
    # 被筛掉的组不是"统计为 0",而是这一格里一个样本都没有。
    assert groups["rule"]["t1_close"]["planned_count"] == 0
    assert groups["rule"]["t1_close"]["available"] is False


def test_returns_summary_group_filter_uses_group_name_parameter(experiment_client):
    """收益接口的组参数叫 group_name(台账叫 group),前端传错会一片空白。"""
    groups = experiment_client.get(
        "/api/returns/summary",
        params={"as_of": "20260804", "group_name": "hybrid"},
    ).json()["groups"]

    assert groups["hybrid"]["t1_close"]["average"] == -0.1
    assert groups["ai"]["t1_close"]["planned_count"] == 0


def test_returns_detail_filters_by_signal_date_and_entry_status(experiment_client):
    def rows(**params) -> list[dict]:
        response = experiment_client.get("/api/returns", params=params)
        assert response.status_code == 200
        return response.json()["items"]

    filled = rows(as_of="20260804", entry_status="filled", horizon="t1_close")
    assert {row["group_name"] for row in filled} == {"ai", "hybrid"}
    # 老批次只缺涨跌停价:算过收益,但成交结果还没定。
    pending = rows(as_of="20260803", entry_status="pending_entry", horizon="t1_close")
    assert {row["run_id"] for row in pending} == {"run-20260803"}
    assert all(row["entry_price"] is None for row in pending)
    # 信号日与成交状态是两个独立条件,交集为空时如实返回空列表。
    assert rows(as_of="20260803", entry_status="filled") == []