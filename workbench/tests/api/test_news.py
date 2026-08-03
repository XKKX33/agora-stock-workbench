"""舆情 API 的接口测试。

夹具库(tests/api/conftest.py)只灌了行情与一次离线扫描,**没有登记任何
舆情来源**。这正是要锁住的状态:接口必须如实报"来源没登记",而不是回一个
空列表让页面显示成"今天没新闻"。

运行:
    cd workbench
    python -m pytest tests/api/test_news.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.db import Store
from tests.test_run_scan_offline import AS_OF

pytestmark = pytest.mark.api


def _register_news(db_path, *, trade_date: str = AS_OF) -> None:
    """往隔离的临时库里塞一条来源 + 一条舆情 + 一条关联。

    只在测试库上做,真实库不碰。字段与 engine/db.py 的建表语句一一对应。
    """
    with Store(db_path, ensure_schema=True) as store:
        store.upsert_news_sources(
            pd.DataFrame(
                [
                    {
                        "source_id": "test_src",
                        "name": "测试来源",
                        "kind": "notice",
                        "home_url": "https://example.com",
                        "base_credibility": 0.9,
                        "compliance_note": "测试夹具,不做真实抓取",
                        "enabled": True,
                    }
                ]
            )
        )
        store.upsert_news_items(
            pd.DataFrame(
                [
                    {
                        "news_id": "news-001",
                        "source_id": "test_src",
                        "title": "某公司发布年度业绩预增公告",
                        "summary": "预计净利润同比增长 40%~60%",
                        "url": "https://example.com/notice/1",
                        "published_at": "2026-07-31T09:30:00",
                        "fetched_at": "2026-07-31T15:05:00",
                        "trade_date": trade_date,
                        "dedup_key": "dk-001",
                        "duplicate_of": None,
                        "event_type": "业绩",
                        "sentiment": "positive",
                        "sentiment_score": 0.6,
                        "credibility": 0.85,
                        "raw_json": "{}",
                    }
                ]
            )
        )
        store.upsert_news_links(
            pd.DataFrame(
                [
                    {
                        "news_id": "news-001",
                        "link_type": "stock",
                        "link_key": "600001.SH",
                        "match_basis": "code_in_text",
                        "match_text": "600001",
                        "confidence": 0.95,
                    }
                ]
            )
        )


# ---------------------------------------------------------------- 缺数据状态


def test_news_without_sources_reports_missing_reason(client):
    """没登记来源 ≠ 今天没新闻。接口要说清缺在哪一环。"""
    response = client.get("/api/news")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_source_registered"
    assert payload["items"] == []
    assert payload["detail"]


def test_news_sources_empty_is_reported_not_silent(client):
    response = client.get("/api/news/sources")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_source_registered"


def test_stock_news_without_sources_reports_missing_reason(client):
    response = client.get("/api/news/stocks/600001.SH")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_source_registered"


def test_never_collected_differs_from_no_news_on_date(client, db_path):
    """登记了来源但没采过,原因是 never_collected,不是 no_news_on_date。"""
    with Store(db_path, ensure_schema=True) as store:
        store.upsert_news_sources(
            pd.DataFrame(
                [
                    {
                        "source_id": "test_src",
                        "name": "测试来源",
                        "kind": "notice",
                        "home_url": "https://example.com",
                        "base_credibility": 0.9,
                        "compliance_note": "测试夹具",
                        "enabled": True,
                    }
                ]
            )
        )

    payload = client.get("/api/news").json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "never_collected"


# ---------------------------------------------------------------- 有数据


def test_news_digest_returns_source_and_traceable_link(client, db_path):
    """每条研判都要能追溯到来源与原始链接。"""
    _register_news(db_path)

    payload = client.get("/api/news").json()
    assert payload["available"] is True
    assert payload["trade_date"] == AS_OF
    item = payload["items"][0]
    assert item["title"]
    assert item["url"] == "https://example.com/notice/1"
    assert item["source"]["source_id"] == "test_src"
    # 情绪与事件分类是待验证判断,必须标出来,不能与原文事实混为一谈
    assert item["judgement"]["label"] == "unverified"
    assert item["judgement"]["sentiment"] == "positive"
    # 合规备注要一路带到页面
    assert payload["sources"][0]["compliance_note"]


def test_news_defaults_to_latest_collected_date(client, db_path):
    """省略 trade_date 时取舆情库最新一天,不拿行情日期去查。"""
    _register_news(db_path, trade_date="20200101")

    payload = client.get("/api/news").json()
    assert payload["available"] is True
    assert payload["trade_date"] == "20200101"


def test_stock_news_carries_match_basis(client, db_path):
    """凭什么说这条新闻跟这只票有关,必须能当场查。"""
    _register_news(db_path)

    payload = client.get("/api/news/stocks/600001.SH").json()
    assert payload["available"] is True
    link = payload["items"][0]["link"]
    assert link["match_basis"] == "code_in_text"
    assert link["match_text"] == "600001"


def test_linked_news_carries_source_home_url(client, db_path):
    """关联查询的来源信息必须与列表查询一样完整。

    回归用:`news_for_link` 的 SQL 一度漏选 `s.home_url AS source_home_url`,
    而两条路径共用 `_news_row()` 读这个列名。pandas 的 Series.get 对缺失键
    返回 None 而不报错,于是每条关联舆情都静默变成 home_url=null——按本项目
    的约定,null 表示"缺失",等于把已有的来源主页讲成没有。这类错误不会抛异常,
    只能靠断言锁住。
    """
    _register_news(db_path)

    payload = client.get("/api/news/stocks/600001.SH").json()
    assert payload["available"] is True
    source = payload["items"][0]["source"]
    assert source["home_url"] == "https://example.com"
    assert source["name"] == "测试来源"
    assert source["kind"] == "notice"


def test_unlinked_stock_reports_no_linked_news(client, db_path):
    """采过但这只票没关联上,与"从没采过"是两回事。"""
    _register_news(db_path)

    payload = client.get("/api/news/stocks/000999.SZ").json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_linked_news"


def test_as_of_filters_future_news(client, db_path):
    """前视纪律:复盘 T 日不该读到 T+1 的新闻。"""
    _register_news(db_path, trade_date="20991231")

    payload = client.get(
        "/api/news/stocks/600001.SH", params={"as_of": AS_OF}
    ).json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_linked_news"


def test_limit_is_bounded(client):
    """超范围的 limit 由 FastAPI 拦下,不落到 SQL。"""
    assert client.get("/api/news", params={"limit": 0}).status_code == 422
    assert client.get("/api/news", params={"limit": 9999}).status_code == 422


# ---------------------------------------------------------------- 行业板块总览


def _industry_item(
    news_id: str,
    title: str,
    trade_date: str,
    sentiment,
) -> dict:
    """构造一条测试舆情原文行,字段与 engine/db.py 建表一一对应。"""
    return {
        "news_id": news_id,
        "source_id": "test_src",
        "title": title,
        "summary": "行业板块聚合测试摘要",
        "url": f"https://example.com/{news_id}",
        "published_at": f"{trade_date}T09:30:00",
        "fetched_at": f"{trade_date}T15:05:00",
        "trade_date": trade_date,
        "dedup_key": f"dk-{news_id}",
        "duplicate_of": None,
        "event_type": None,
        "sentiment": sentiment,
        "sentiment_score": None if sentiment is None else 0.5,
        "credibility": 0.8,
        "raw_json": "{}",
    }


def _register_industry_news(
    db_path, *, main_date: str = AS_OF, old_date: str = "20200101"
) -> None:
    """灌入两个行业、两个交易日、一条未匹配行业的舆情(只在测试库上做)。"""
    with Store(db_path, ensure_schema=True) as store:
        store.upsert_news_sources(
            pd.DataFrame(
                [
                    {
                        "source_id": "test_src",
                        "name": "测试来源",
                        "kind": "news",
                        "home_url": "https://example.com",
                        "base_credibility": 0.9,
                        "compliance_note": "测试夹具,不做真实抓取",
                        "enabled": True,
                    }
                ]
            )
        )
        store.upsert_news_items(
            pd.DataFrame(
                [
                    _industry_item("news-i1", "互联网板块正面新闻", main_date, "positive"),
                    _industry_item("news-i2", "互联网板块负面新闻", main_date, "negative"),
                    _industry_item("news-i3", "银行板块中性新闻", main_date, "neutral"),
                    _industry_item("news-i4", "没有行业关联的新闻", main_date, None),
                    _industry_item("news-i6", "互联网板块未判定新闻", main_date, None),
                    _industry_item("news-i5", "更早一天的互联网新闻", old_date, "positive"),
                ]
            )
        )
        store.upsert_news_links(
            pd.DataFrame(
                [
                    {
                        "news_id": nid,
                        "link_type": "industry",
                        "link_key": industry,
                        "match_basis": "industry_name_in_text",
                        "match_text": industry,
                        "confidence": 0.9,
                    }
                    for nid, industry in [
                        ("news-i1", "互联网"),
                        ("news-i2", "互联网"),
                        ("news-i6", "互联网"),
                        ("news-i3", "银行"),
                        ("news-i5", "互联网"),
                    ]
                ]
            )
        )


def test_industry_overview_without_sources_reports_missing_reason(client):
    """没登记来源时,行业总览必须与列表页一样说清缺在哪一环。"""
    payload = client.get("/api/news/industries").json()

    assert payload["available"] is False
    assert payload["missing_reason"] == "no_source_registered"
    assert payload["industries"] == []
    assert payload["unlinked_count"] is None


def test_industry_overview_never_collected_differs_from_no_news(client, db_path):
    """登记了来源但没采过,行业总览同样报 never_collected。"""
    with Store(db_path, ensure_schema=True) as store:
        store.upsert_news_sources(
            pd.DataFrame(
                [
                    {
                        "source_id": "test_src",
                        "name": "测试来源",
                        "kind": "news",
                        "home_url": "https://example.com",
                        "base_credibility": 0.9,
                        "compliance_note": "测试夹具",
                        "enabled": True,
                    }
                ]
            )
        )

    payload = client.get("/api/news/industries").json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "never_collected"


def test_industry_overview_no_news_on_date_is_explicit(client, db_path):
    """指定一个没有条目的交易日,报 no_news_on_date,不是空列表。"""
    _register_industry_news(db_path)

    payload = client.get(
        "/api/news/industries", params={"trade_date": "20991231"}
    ).json()
    assert payload["available"] is False
    assert payload["missing_reason"] == "no_news_on_date"


def test_industry_overview_groups_by_industry(client, db_path):
    """按行业板块聚合:条数去重计数,情绪分布逐项如实,未匹配如实单列。"""
    _register_industry_news(db_path)

    payload = client.get("/api/news/industries").json()
    assert payload["available"] is True
    assert payload["trade_date"] == AS_OF
    by_name = {group["industry"]: group for group in payload["industries"]}
    assert set(by_name) == {"互联网", "银行"}

    web = by_name["互联网"]
    assert web["news_count"] == 3
    assert web["sentiment"] == {
        "positive": 1,
        "negative": 1,
        "neutral": 0,
        "undecided": 1,
    }
    bank = by_name["银行"]
    assert bank["news_count"] == 1
    assert bank["sentiment"]["neutral"] == 1
    # 没有行业关联的新闻不硬塞进任何板块,单独计数
    assert payload["unlinked_count"] == 1


def test_industry_overview_respects_trade_date(client, db_path):
    """指定交易日后,总览只看那一天,未匹配条数同步变化。"""
    _register_industry_news(db_path)

    payload = client.get(
        "/api/news/industries", params={"trade_date": "20200101"}
    ).json()
    assert payload["available"] is True
    assert payload["trade_date"] == "20200101"
    assert [group["industry"] for group in payload["industries"]] == ["互联网"]
    assert payload["industries"][0]["news_count"] == 1
    assert payload["unlinked_count"] == 0


def test_industry_news_filters_by_trade_date(client, db_path):
    """板块下钻接口支持只看指定交易日,并保留匹配依据。"""
    _register_industry_news(db_path)

    payload = client.get(
        "/api/news/industries/互联网", params={"trade_date": AS_OF}
    ).json()
    assert payload["available"] is True
    assert payload["link_key"] == "互联网"
    assert len(payload["items"]) == 3
    assert all(
        item["link"]["match_basis"] == "industry_name_in_text"
        for item in payload["items"]
    )


def test_industry_news_without_date_returns_all_history(client, db_path):
    """不带 trade_date 时返回该行业全部历史关联,保持向后兼容。"""
    _register_industry_news(db_path)

    payload = client.get("/api/news/industries/互联网").json()
    assert payload["available"] is True
    assert len(payload["items"]) == 4


def test_industry_overview_route_not_shadowed_by_single_industry(client, db_path):
    """/news/industries 总览路由不能被单行业路由吃掉。"""
    _register_industry_news(db_path)

    overview = client.get("/api/news/industries").json()
    assert overview["available"] is True
    assert overview["trade_date"] == AS_OF

    single = client.get("/api/news/industries/银行").json()
    assert single["available"] is True
    assert single["link_key"] == "银行"
