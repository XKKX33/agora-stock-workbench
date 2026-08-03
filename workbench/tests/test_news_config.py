"""舆情配置加载与采集器装配的单测。

这一层要锁住的是"配置写错时会不会静默降级"。三条:

- 缺 news 段 / enabled=false -> 明确的关闭状态,不是失败,也不是"采到 0 条"。
- 数值、时间、可信度写错 -> 启动时抛错,不回退默认值。
- 配置引用了没实现的采集器 -> 抛错,不跳过。跳过会让页面显示"今天没有舆情",
  而真相是采集器根本没接上。

运行:
    cd workbench
    python -m pytest tests/test_news_config.py -q
"""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.news import NewsCollectError, NewsSource, RawNewsItem  # noqa: E402
from engine.news_config import (  # noqa: E402
    FETCHER_REGISTRY,
    NewsConfigError,
    build_fetchers,
    load_news_config,
    parse_close_cutoff,
)

pytestmark = pytest.mark.unit


def _source_dict(**kw) -> dict:
    base = {
        "source_id": "demo",
        "name": "示例来源",
        "kind": "news",
        "home_url": "https://example.com",
        "base_credibility": 0.9,
        "compliance_note": "官方公开接口,已核验",
        "enabled": True,
        "fetcher": "demo_fetcher",
        "options": {},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 关闭状态


def test_missing_news_section_is_disabled_not_error():
    """缺 news 段是明确的关闭状态,任务链会如实报"未配置"。"""
    config = load_news_config({})
    assert config.enabled is False
    assert config.sources == ()
    assert config.enabled_sources == ()


def test_disabled_config_keeps_declared_sources_visible():
    """关闭时来源声明仍要读出来:页面要能解释"配了但没开"。"""
    config = load_news_config(
        {"news": {"enabled": False, "sources": [_source_dict(enabled=False)]}}
    )
    assert config.enabled is False
    assert len(config.sources) == 1
    assert config.enabled_sources == ()
    assert config.as_dict()["sources"][0]["compliance_note"]


def test_defaults_match_engine_constants():
    config = load_news_config({"news": {}})
    assert config.close_cutoff == time(15, 0)
    assert config.half_life_days == pytest.approx(3.0)


# ---------------------------------------------------------------- 严格校验


@pytest.mark.parametrize("raw", ["15:00", "9:30", time(15, 0)])
def test_parse_close_cutoff_accepts_valid(raw):
    assert isinstance(parse_close_cutoff(raw), time)


@pytest.mark.parametrize("raw", ["1500", "下午三点", "25:00", "15:99", ""])
def test_parse_close_cutoff_rejects_invalid(raw):
    """切错一分钟,归属交易日就整体错位,所以不许兜底。"""
    with pytest.raises(NewsConfigError):
        parse_close_cutoff(raw)


@pytest.mark.parametrize("value", [0, -1, "abc"])
def test_bad_half_life_raises(value):
    with pytest.raises(NewsConfigError):
        load_news_config({"news": {"half_life_days": value}})


def test_news_section_must_be_mapping():
    with pytest.raises(NewsConfigError):
        load_news_config({"news": ["a"]})


def test_sources_must_be_list():
    with pytest.raises(NewsConfigError):
        load_news_config({"news": {"sources": {"a": 1}}})


def test_source_must_declare_fetcher():
    item = _source_dict()
    item.pop("fetcher")
    with pytest.raises(NewsConfigError) as excinfo:
        load_news_config({"news": {"sources": [item]}})
    assert "fetcher" in str(excinfo.value)


def test_source_without_compliance_note_is_rejected():
    """合规备注缺失直接拒绝登记——这是采集前唯一的人工闸门。"""
    with pytest.raises(NewsCollectError):
        load_news_config({"news": {"sources": [_source_dict(compliance_note="")]}})


@pytest.mark.parametrize("value", [1.5, -0.2, "很高"])
def test_bad_credibility_raises(value):
    with pytest.raises(NewsConfigError):
        load_news_config({"news": {"sources": [_source_dict(base_credibility=value)]}})


def test_blank_credibility_means_unassessed():
    """留空 = 未评估 = None,不给 0.5 冒充"中等可信"。"""
    config = load_news_config({"news": {"sources": [_source_dict(base_credibility=None)]}})
    assert config.sources[0].source.base_credibility is None


def test_duplicate_source_id_raises():
    with pytest.raises(NewsConfigError) as excinfo:
        load_news_config(
            {"news": {"sources": [_source_dict(), _source_dict(name="另一个")]}}
        )
    assert "重复" in str(excinfo.value)


def test_options_must_be_mapping():
    with pytest.raises(NewsConfigError):
        load_news_config({"news": {"sources": [_source_dict(options=[1, 2])]}})


# ---------------------------------------------------------------- 装配


def test_unknown_fetcher_raises_instead_of_skipping():
    """引用未实现的采集器必须报错。静默跳过会让"没接上"看起来像"今天没消息"。"""
    config = load_news_config({"news": {"enabled": True, "sources": [_source_dict()]}})
    with pytest.raises(NewsConfigError) as excinfo:
        build_fetchers(config)
    message = str(excinfo.value)
    assert "demo_fetcher" in message
    assert "合规核验" in message


def test_registry_registers_trendradar_only():
    """注册表当前只有 trendradar(已过合规核验、GPL 源码隔离在 vendor/)。

    这条锁住"注册表是白名单":任何新采集器上线前须先过合规核验再登记,
    不允许悄悄多出一个没审过的键。
    """
    assert set(FETCHER_REGISTRY) == {"trendradar"}
    assert callable(FETCHER_REGISTRY["trendradar"])


def test_build_fetchers_uses_registered_factory():
    """注册后按 fetcher 键装配,options 原样透传。"""

    class _Fetcher:
        def __init__(self, source: NewsSource, options: dict):
            self._source = source
            self.options = options

        @property
        def source(self) -> NewsSource:
            return self._source

        def fetch(self, *, trade_date, window_start, window_end):
            return [
                RawNewsItem(
                    source_id=self._source.source_id,
                    title="示例",
                    url="https://example.com/1",
                    published_at="2026-07-31 10:00:00",
                )
            ]

    FETCHER_REGISTRY["demo_fetcher"] = _Fetcher
    try:
        config = load_news_config(
            {
                "news": {
                    "enabled": True,
                    "sources": [_source_dict(options={"timeout": 5})],
                }
            }
        )
        fetchers = build_fetchers(config)
        assert len(fetchers) == 1
        assert fetchers[0].source.source_id == "demo"
        assert fetchers[0].options == {"timeout": 5}
    finally:
        FETCHER_REGISTRY.pop("demo_fetcher", None)
    assert "demo_fetcher" not in FETCHER_REGISTRY


def test_build_fetchers_includes_disabled_sources():
    """未启用来源也要装配出来:collect_news 会登记它们以解释"为什么没采"。"""

    class _Fetcher:
        def __init__(self, source: NewsSource, options: dict):
            self._source = source

        @property
        def source(self) -> NewsSource:
            return self._source

        def fetch(self, *, trade_date, window_start, window_end):
            raise AssertionError("未启用来源不该被调用")

    FETCHER_REGISTRY["demo_fetcher"] = _Fetcher
    try:
        config = load_news_config(
            {"news": {"enabled": True, "sources": [_source_dict(enabled=False)]}}
        )
        assert config.enabled_sources == ()
        assert len(build_fetchers(config)) == 1
    finally:
        FETCHER_REGISTRY.pop("demo_fetcher", None)
