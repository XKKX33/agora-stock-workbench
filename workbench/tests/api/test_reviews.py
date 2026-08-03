"""复盘 API 的接口测试。

最要紧的一条是 test_review_get_does_not_write:GET /api/reviews 是读接口,
刷新页面不该把 retN 悄悄回填进台账。这条测试如果失败,说明读路径又漏写了。

运行:
    cd workbench
    python -m pytest tests/api/test_reviews.py -q
"""

from __future__ import annotations

import pytest

from engine.db import Store
from tests.test_run_scan_offline import AS_OF

pytestmark = pytest.mark.api


def test_review_returns_labeled_sections(client):
    response = client.get("/api/reviews")

    assert response.status_code == 200
    payload = response.json()
    sections = payload["sections"]
    assert sections
    for section in sections.values():
        # 每一节要么有数据,要么说清为什么没有——不允许空着蒙混过去
        assert section["label"] in {"fact", "derived", "unverified"}
        if section["available"]:
            assert section["data"] is not None
        else:
            assert section["missing_reason"]
            assert section["detail"]


def test_review_accepts_explicit_trade_date(client):
    payload = client.get("/api/reviews", params={"trade_date": AS_OF}).json()

    assert payload["trade_date"] == AS_OF


def test_review_get_does_not_write(client, db_path):
    """打开复盘页不该改库:调用前后待回填条数必须一致。"""
    with Store(db_path, ensure_schema=False) as store:
        before = len(store.open_picks_awaiting_return("ret1"))

    assert client.get("/api/reviews").status_code == 200

    with Store(db_path, ensure_schema=False) as store:
        after = len(store.open_picks_awaiting_return("ret1"))
    assert after == before


def test_prediction_review_is_read_only_mode(client):
    """读接口下 pending_reasons 为 null:原因要走一遍日历才知道,不猜。"""
    payload = client.get("/api/reviews").json()
    section = payload["sections"]["prediction_review"]
    if not section["available"]:
        pytest.skip(f"夹具无可回看批次: {section['missing_reason']}")
    backfill = section["data"]["backfill"]
    assert backfill["mode"] == "read_only"
    assert backfill["pending_reasons"] is None


def test_news_section_reports_missing_source(client):
    """夹具没登记舆情来源,重要舆情一节要报 no_source_registered。"""
    payload = client.get("/api/reviews").json()
    section = payload["sections"]["news_highlights"]

    assert section["available"] is False
    assert section["missing_reason"] == "no_source_registered"
