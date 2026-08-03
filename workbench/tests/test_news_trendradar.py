"""TrendRadar 采集适配器的离线单测。

这一层锁住的是"把 vendor 的热榜结构翻译成 RawNewsItem"这段纯转换,以及围绕
它的合规/诚实约束。全程离线:用 monkeypatch 把 `_load_data_fetcher_class`
换成一个进程内假 DataFetcher,绝不联网、绝不触碰真实库。

锁定的行为:
- 平台清单、api_url、代理、请求间隔全部来自 options,不写死。
- 每条 RawNewsItem 字段完整,published_at 能被下游 parse_published_at 解析。
- raw.time_basis == "first_seen_at_collect":采集时刻不冒充发布时间。
- summary 恒为 None(热榜无正文),declared_codes 恒为空(热榜无结构化代码)。
- 无链接条目被丢弃(每条舆情必须可回溯到原文)。
- 全部平台失败 -> 抛错,不把空结果伪装成"今天没热点"。
- vendor 文件缺失 -> 抛错,不静默降级。
- 域名安全规则从 platforms 的 expected_domain 提取,原样传给 DataFetcher。

另有一条走 collect_news 的集成测试,断言下游 link_stocks 确实能从热榜标题里
关联出对应 ts_code——这正是"轻量接入"的关键:适配器只出标题,关联是白送的。

运行:
    cd workbench
    python -m pytest tests/test_news_trendradar.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import news_trendradar  # noqa: E402
from engine.db import Store  # noqa: E402
from engine.news import NewsSource, collect_news  # noqa: E402
from engine.news_text import parse_published_at  # noqa: E402
from engine.news_trendradar import (  # noqa: E402
    TIME_BASIS,
    TrendRadarConfigError,
    TrendRadarFetcher,
    build_trendradar_fetcher,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 假 DataFetcher

class FakeDataFetcher:
    """进程内替身:记录调用参数,返回构造好的热榜结构,不发任何网络请求。

    结构与 vendor DataFetcher.crawl_websites 的返回一致:
        (results, id_to_name, failed_ids)
    其中 results = {platform_id: {title: {"ranks": [...], "url": ..., "mobileUrl": ...}}}
    """

    # 类级别的调用记录,便于测试断言构造参数(实例由被测代码内部创建)
    last_init: dict = {}
    last_crawl: dict = {}
    results: dict = {}
    id_to_name: dict = {}
    failed_ids: list = []

    def __init__(self, proxy_url=None, api_url=None):
        FakeDataFetcher.last_init = {"proxy_url": proxy_url, "api_url": api_url}

    def crawl_websites(self, ids_list, request_interval=100, domain_rules=None):
        FakeDataFetcher.last_crawl = {
            "ids_list": list(ids_list),
            "request_interval": request_interval,
            "domain_rules": dict(domain_rules or {}),
        }
        return (
            dict(FakeDataFetcher.results),
            dict(FakeDataFetcher.id_to_name),
            list(FakeDataFetcher.failed_ids),
        )


def _install_fake(monkeypatch, *, results, id_to_name=None, failed_ids=None):
    """把 _load_data_fetcher_class 换成返回 FakeDataFetcher 的桩,全程离线。"""
    FakeDataFetcher.results = results
    FakeDataFetcher.id_to_name = id_to_name or {}
    FakeDataFetcher.failed_ids = failed_ids or []
    FakeDataFetcher.last_init = {}
    FakeDataFetcher.last_crawl = {}
    monkeypatch.setattr(
        news_trendradar, "_load_data_fetcher_class", lambda: FakeDataFetcher
    )


def _source(source_id="trendradar", *, enabled=True) -> NewsSource:
    return NewsSource(
        source_id=source_id,
        name="TrendRadar 全网热榜",
        kind="news",
        home_url="https://github.com/sansan0/TrendRadar",
        base_credibility=None,
        compliance_note=(
            "经 newsnow 公开聚合 API 获取全网热榜标题;热榜快照无权威发布时间,"
            "published_at 记为首次抓取时刻;核验于 2026-08-01"
        ),
        enabled=enabled,
    )


_PLATFORMS = [
    {"id": "weibo", "name": "微博", "expected_domain": "weibo.com"},
    {"id": "cls-hot", "name": "财联社热门", "expected_domain": "cls.cn"},
]


def _options(**kw) -> dict:
    base = {
        "platforms": _PLATFORMS,
        "api_url": "",
        "proxy_url": "",
        "request_interval_ms": 1000,
    }
    base.update(kw)
    return base


def _fetch(monkeypatch, *, options=None, results=None, **install_kw):
    """跑一次 fetch,返回条目列表。窗口参数对热榜无意义,给占位值。"""
    _install_fake(monkeypatch, results=results if results is not None else {}, **install_kw)
    fetcher = TrendRadarFetcher(_source(), options or _options())
    return fetcher.fetch(
        trade_date="20260731",
        window_start=datetime(2026, 7, 30, 15, 0),
        window_end=datetime(2026, 7, 31, 15, 0),
    )


# ---------------------------------------------------------------- 配置解析


def test_platforms_are_read_from_options():
    """平台清单来自 options,不写死。"""
    fetcher = TrendRadarFetcher(_source(), _options())
    assert fetcher._platforms == ("weibo", "cls-hot")


def test_domain_rules_extracted_from_platforms():
    """expected_domain 提取成域名安全规则,供 DataFetcher 校验链接。"""
    fetcher = TrendRadarFetcher(_source(), _options())
    assert fetcher._domain_rules == {"weibo": "weibo.com", "cls-hot": "cls.cn"}


def test_platforms_accept_bare_string_ids():
    """平台条目也可以是裸字符串 id(无域名校验)。"""
    fetcher = TrendRadarFetcher(_source(), _options(platforms=["weibo", "zhihu"]))
    assert fetcher._platforms == ("weibo", "zhihu")
    assert fetcher._domain_rules == {}


def test_missing_platforms_raises():
    """缺平台清单意味着采不到任何东西,属配置错误,显式抛而不是空跑。"""
    with pytest.raises(TrendRadarConfigError) as excinfo:
        TrendRadarFetcher(_source(), _options(platforms=None))
    assert "platforms" in str(excinfo.value)


def test_empty_platforms_list_raises():
    with pytest.raises(TrendRadarConfigError):
        TrendRadarFetcher(_source(), _options(platforms=[]))


def test_platform_entry_without_id_raises():
    with pytest.raises(TrendRadarConfigError):
        TrendRadarFetcher(_source(), _options(platforms=[{"name": "无 id"}]))


@pytest.mark.parametrize("value", ["abc", -5, 1.5])
def test_bad_request_interval_raises(value):
    with pytest.raises(TrendRadarConfigError):
        TrendRadarFetcher(_source(), _options(request_interval_ms=value))


def test_interval_defaults_when_omitted():
    """留空用 100ms(TrendRadar 默认),不报错。"""
    opts = _options()
    opts.pop("request_interval_ms")
    fetcher = TrendRadarFetcher(_source(), opts)
    assert fetcher._request_interval == 100


def test_blank_api_and_proxy_become_none():
    """空串 api_url/proxy_url 归一成 None,让 vendor 用它自己的默认地址。"""
    fetcher = TrendRadarFetcher(_source(), _options(api_url="  ", proxy_url=""))
    assert fetcher._api_url is None
    assert fetcher._proxy_url is None


# ---------------------------------------------------------------- 转换


def test_fetch_converts_hotlist_to_raw_items(monkeypatch):
    """热榜结构逐条转成字段完整的 RawNewsItem。"""
    results = {
        "weibo": {
            "某上市公司午后涨停": {
                "ranks": [1, 2],
                "url": "https://s.weibo.com/weibo?q=1",
                "mobileUrl": "https://m.weibo.cn/1",
            }
        }
    }
    items = _fetch(monkeypatch, results=results, id_to_name={"weibo": "微博"})

    assert len(items) == 1
    item = items[0]
    assert item.source_id == "trendradar"
    assert item.title == "某上市公司午后涨停"
    assert item.url == "https://s.weibo.com/weibo?q=1"
    # 热榜没有正文与结构化代码,恒为空,不编造
    assert item.summary is None
    assert item.declared_codes == ()
    # raw 保留可审计的溯源信息
    assert item.raw["platform_id"] == "weibo"
    assert item.raw["platform_name"] == "微博"
    assert item.raw["ranks"] == [1, 2]
    assert item.raw["mobile_url"] == "https://m.weibo.cn/1"
    assert item.raw["provider"] == "trendradar+newsnow"


def test_published_at_is_collect_time_and_parseable(monkeypatch):
    """published_at 记为采集时刻,且能被下游 parse_published_at 解析。"""
    before = datetime.now().replace(microsecond=0)
    results = {"weibo": {"标题": {"ranks": [1], "url": "https://weibo.com/1"}}}
    items = _fetch(monkeypatch, results=results)
    after = datetime.now()

    parsed = parse_published_at(items[0].published_at)
    # 采集时刻应落在本次调用前后之间(容忍 1 秒 timespec 截断)
    assert before <= parsed.replace(microsecond=0) <= after


def test_time_basis_marks_snapshot_not_publish_time(monkeypatch):
    """raw.time_basis 显式标注这是"首次抓取时刻",不冒充发布时间。"""
    results = {"weibo": {"标题": {"ranks": [1], "url": "https://weibo.com/1"}}}
    items = _fetch(monkeypatch, results=results)
    assert items[0].raw["time_basis"] == TIME_BASIS == "first_seen_at_collect"


def test_items_without_url_are_dropped(monkeypatch):
    """无链接条目丢弃:每条舆情必须能回溯到原文。"""
    results = {
        "weibo": {
            "有链接": {"ranks": [1], "url": "https://weibo.com/1"},
            "无链接": {"ranks": [2], "url": "", "mobileUrl": ""},
        }
    }
    items = _fetch(monkeypatch, results=results)
    assert [i.title for i in items] == ["有链接"]


def test_mobile_url_used_when_url_missing(monkeypatch):
    """正式链接缺失时退回移动端链接,而不是直接丢弃。"""
    results = {
        "weibo": {"仅移动端": {"ranks": [1], "url": "", "mobileUrl": "https://m.weibo.cn/9"}}
    }
    items = _fetch(monkeypatch, results=results)
    assert len(items) == 1
    assert items[0].url == "https://m.weibo.cn/9"


def test_multiple_platforms_are_flattened(monkeypatch):
    """多平台结果扁平化成一个条目列表,各自带回自己的 platform_id。"""
    results = {
        "weibo": {"微博热点": {"ranks": [1], "url": "https://weibo.com/1"}},
        "cls-hot": {"财联社快讯": {"ranks": [1], "url": "https://cls.cn/1"}},
    }
    items = _fetch(
        monkeypatch,
        results=results,
        id_to_name={"weibo": "微博", "cls-hot": "财联社热门"},
    )
    platforms = {i.raw["platform_id"] for i in items}
    assert platforms == {"weibo", "cls-hot"}


def test_empty_results_return_empty_list(monkeypatch):
    """全平台在榜但无匹配/无条目 -> 空列表(真实的"此刻没热点"),不抛错。"""
    items = _fetch(monkeypatch, results={})
    assert items == []


# ---------------------------------------------------------------- 参数透传


def test_options_passed_through_to_data_fetcher(monkeypatch):
    """api_url / proxy_url / 平台清单 / 请求间隔 / 域名规则原样传给 vendor。"""
    _fetch(
        monkeypatch,
        options=_options(api_url="https://my.newsnow/api/s", proxy_url="http://127.0.0.1:1080"),
        results={},
    )
    assert FakeDataFetcher.last_init == {
        "proxy_url": "http://127.0.0.1:1080",
        "api_url": "https://my.newsnow/api/s",
    }
    crawl = FakeDataFetcher.last_crawl
    assert crawl["ids_list"] == ["weibo", "cls-hot"]
    assert crawl["request_interval"] == 1000
    assert crawl["domain_rules"] == {"weibo": "weibo.com", "cls-hot": "cls.cn"}


# ---------------------------------------------------------------- 失败暴露


def test_all_platforms_failed_raises(monkeypatch):
    """全部平台抓取失败 -> 抛错,不把空结果伪装成"今天没有热点"。"""
    _install_fake(monkeypatch, results={}, failed_ids=["weibo", "cls-hot"])
    fetcher = TrendRadarFetcher(_source(), _options())
    with pytest.raises(TrendRadarConfigError) as excinfo:
        fetcher.fetch(
            trade_date="20260731",
            window_start=datetime(2026, 7, 30, 15, 0),
            window_end=datetime(2026, 7, 31, 15, 0),
        )
    assert "全部平台抓取失败" in str(excinfo.value)


def test_partial_failure_still_returns_successful_platforms(monkeypatch):
    """部分平台失败但有成功结果时,不抛错,返回成功平台的条目。"""
    results = {"weibo": {"标题": {"ranks": [1], "url": "https://weibo.com/1"}}}
    _install_fake(monkeypatch, results=results, failed_ids=["cls-hot"])
    fetcher = TrendRadarFetcher(_source(), _options())
    items = fetcher.fetch(
        trade_date="20260731",
        window_start=datetime(2026, 7, 30, 15, 0),
        window_end=datetime(2026, 7, 31, 15, 0),
    )
    assert len(items) == 1


def test_missing_vendor_file_raises(monkeypatch):
    """vendor 文件缺失是需要人处理的真实故障,显式抛而非静默降级。"""
    monkeypatch.setattr(
        news_trendradar, "_FETCHER_PATH", Path("/nonexistent/trendradar/fetcher.py")
    )
    with pytest.raises(TrendRadarConfigError) as excinfo:
        news_trendradar._load_data_fetcher_class()
    assert "未找到" in str(excinfo.value)


# ---------------------------------------------------------------- 工厂


def test_factory_builds_fetcher():
    fetcher = build_trendradar_fetcher(_source(), _options())
    assert isinstance(fetcher, TrendRadarFetcher)
    assert fetcher.source.source_id == "trendradar"


# ---------------------------------------------------------------- 与 collect_news 的集成

_CAL_DATES = [
    ("20260728", 1),
    ("20260729", 1),
    ("20260730", 1),
    ("20260731", 1),
    ("20260803", 1),
]
_STOCKS = [
    ("601012.SH", "601012", "隆基绿能", "光伏设备"),
    ("600519.SH", "600519", "贵州茅台", "白酒"),
]


def _seed(store: Store) -> None:
    store.upsert(
        "trade_cal",
        pd.DataFrame(
            [{"exchange": "SSE", "cal_date": d, "is_open": o} for d, o in _CAL_DATES]
        ),
        keys=("exchange", "cal_date"),
    )
    store.upsert(
        "stock_basic",
        pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "symbol": symbol,
                    "name": name,
                    "area": "",
                    "industry": industry,
                    "market": "主板",
                    "list_date": "20100101",
                }
                for code, symbol, name, industry in _STOCKS
            ]
        ),
        keys=("ts_code",),
    )
    store.upsert(
        "daily",
        pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "trade_date": "20260731",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "pre_close": 10.0,
                    "pct_chg": 2.0,
                    "vol": 1e6,
                    "amount": 1e7,
                }
                for code, _s, _n, _i in _STOCKS
            ]
        ),
        keys=("ts_code", "trade_date"),
    )


def test_collect_news_links_stock_from_hotlist_title(monkeypatch, tmp_path):
    """端到端(离线):热榜标题里出现股票名 -> 下游 link_stocks 自动关联出 ts_code。

    这是"轻量接入"的核心证据:适配器只吐标题,个股关联是既有链路白送的。
    全程隔离库(tmp_path),绝不触碰 workbench/data/market.duckdb。
    """
    results = {
        "cls-hot": {
            "隆基绿能午后涨停 光伏板块集体走强": {
                "ranks": [1],
                "url": "https://www.cls.cn/detail/1",
            }
        }
    }
    _install_fake(monkeypatch, results=results, id_to_name={"cls-hot": "财联社热门"})

    with Store(tmp_path / "tr_integration.duckdb") as store:
        _seed(store)
        fetcher = build_trendradar_fetcher(_source(), _options(platforms=["cls-hot"]))
        result = collect_news(store=store, trade_date="20260731", fetchers=[fetcher])

        assert result.fetched == 1
        assert result.stored == 1

        # 标题里出现"隆基绿能" -> 关联到 601012.SH
        stock_links = store.con.execute(
            "SELECT link_key, match_basis FROM news_links WHERE link_type = 'stock'"
        ).df()
        assert "601012.SH" in stock_links["link_key"].tolist()

        # 行业经关联股票带出光伏设备
        industry_links = store.con.execute(
            "SELECT link_key FROM news_links WHERE link_type = 'industry'"
        ).df()
        assert "光伏设备" in industry_links["link_key"].tolist()

        # 入库条目如实标注时间基准,不冒充发布时间
        raw_json = store.con.execute("SELECT raw_json FROM news_items").fetchone()[0]
        payload = json.loads(raw_json)
        assert payload["source"]["time_basis"] == TIME_BASIS
