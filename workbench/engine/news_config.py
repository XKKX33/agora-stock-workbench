"""舆情来源配置与采集器装配。

把"配置里写了什么"与"实际能采什么"分开:

- `load_news_config` 只负责读 settings.yaml 的 news 段并做严格校验。
- `build_fetchers` 按配置里的 `fetcher` 键到注册表里取具体实现。

**任何一个采集器上线前都必须先完成合规核验**(该站点的 robots.txt / 服务条款 /
是否提供官方接口),核验结论要落到 `compliance_note` 里。当前已注册 `trendradar`
(经 newsnow 公开聚合 API 抓全网热榜,GPL 代码隔离在 vendor/,详见
`news_trendradar.py`)。未完成核验的来源宁可让配置里写了未知采集器时直接报错,
也不放一个"先跑起来再说"的实现进去。

配置里引用了注册表里没有的采集器 -> 抛 NewsConfigError,不静默跳过。
静默跳过会让页面显示"今天没有舆情",而真相是采集器根本没接上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any, Callable, Dict, Optional, Sequence

from .news import NewsFetcher, NewsSource
from .news_text import DEFAULT_CLOSE_CUTOFF, DEFAULT_HALF_LIFE_DAYS
from .news_trendradar import build_trendradar_fetcher

# 采集器工厂注册表:fetcher 键 -> (source, options) -> NewsFetcher 实例。
#
# 每个采集器上线前都必须完成来源合规核验,核验结论落到来源的 compliance_note。
# trendradar 走 newsnow 公开聚合 API 抓全网热榜,GPL 代码隔离在 vendor/,详见
# engine/news_trendradar.py 的模块说明。
FETCHER_REGISTRY: Dict[str, Callable[[NewsSource, dict], NewsFetcher]] = {
    "trendradar": build_trendradar_fetcher,
}


class NewsConfigError(ValueError):
    """舆情配置非法。配置错误必须在启动时暴露,不做默认值兜底。"""


@dataclass(frozen=True)
class NewsSourceConfig:
    """配置文件里的一条来源声明。source 已完成合规备注等校验。"""

    source: NewsSource
    fetcher: str
    options: dict


@dataclass(frozen=True)
class NewsConfig:
    """舆情采集的整体配置。"""

    enabled: bool
    close_cutoff: time
    half_life_days: float
    sources: tuple[NewsSourceConfig, ...]

    @property
    def enabled_sources(self) -> tuple[NewsSourceConfig, ...]:
        return tuple(s for s in self.sources if s.source.enabled)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "close_cutoff": self.close_cutoff.strftime("%H:%M"),
            "half_life_days": self.half_life_days,
            "sources": [
                {
                    "source_id": s.source.source_id,
                    "name": s.source.name,
                    "kind": s.source.kind,
                    "fetcher": s.fetcher,
                    "enabled": s.source.enabled,
                    "compliance_note": s.source.compliance_note,
                }
                for s in self.sources
            ],
        }


def parse_close_cutoff(raw: object) -> time:
    """解析收盘时点。非法值直接抛——切错一分钟,归属交易日就会整体错位。"""
    if isinstance(raw, time):
        return raw
    text = str(raw).strip()
    parts = text.replace("：", ":").split(":")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise NewsConfigError(f"news.close_cutoff 格式非法: {raw!r},应为 HH:MM(例如 15:00)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise NewsConfigError(f"news.close_cutoff 超出合法时间范围: {raw!r}")
    return time(hour=hour, minute=minute)


def load_news_config(settings: dict) -> NewsConfig:
    """从 settings.yaml 的 news 段构造配置。

    缺少 news 段按"未启用且无来源"处理:这是明确的关闭状态,任务链会如实
    报告"未配置舆情来源",而不是假装采了 0 条。
    """
    raw: Any = (settings or {}).get("news") or {}
    if not isinstance(raw, dict):
        raise NewsConfigError(f"news 段应为映射,收到 {type(raw).__name__}")

    half_life = raw.get("half_life_days", DEFAULT_HALF_LIFE_DAYS)
    try:
        half_life = float(half_life)
    except (TypeError, ValueError) as error:
        raise NewsConfigError(f"news.half_life_days 必须为数字,收到 {half_life!r}") from error
    if half_life <= 0:
        raise NewsConfigError(f"news.half_life_days 必须为正,收到 {half_life}")

    declared = raw.get("sources") or []
    if not isinstance(declared, list):
        raise NewsConfigError("news.sources 应为列表")

    sources = tuple(_parse_source(item, index) for index, item in enumerate(declared))
    _reject_duplicate_ids(sources)

    return NewsConfig(
        enabled=bool(raw.get("enabled", False)),
        close_cutoff=parse_close_cutoff(raw.get("close_cutoff", DEFAULT_CLOSE_CUTOFF)),
        half_life_days=half_life,
        sources=sources,
    )


def _parse_source(item: Any, index: int) -> NewsSourceConfig:
    if not isinstance(item, dict):
        raise NewsConfigError(f"news.sources[{index}] 应为映射,收到 {type(item).__name__}")
    fetcher = str(item.get("fetcher") or "").strip()
    if not fetcher:
        raise NewsConfigError(f"news.sources[{index}] 缺少 fetcher(采集器实现名)")

    options = item.get("options") or {}
    if not isinstance(options, dict):
        raise NewsConfigError(f"news.sources[{index}].options 应为映射")

    source = NewsSource(
        source_id=str(item.get("source_id") or "").strip(),
        name=str(item.get("name") or "").strip(),
        kind=str(item.get("kind") or "news").strip(),
        home_url=str(item.get("home_url") or "").strip(),
        base_credibility=_parse_credibility(item.get("base_credibility"), index),
        compliance_note=str(item.get("compliance_note") or ""),
        enabled=bool(item.get("enabled", False)),
    )
    return NewsSourceConfig(source=source, fetcher=fetcher, options=dict(options))


def _parse_credibility(raw: object, index: int) -> Optional[float]:
    """留空就是 None(未评估),不给默认值——0.5 会被当成"评估过、中等可信"。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise NewsConfigError(
            f"news.sources[{index}].base_credibility 必须为 0~1 的数字,收到 {raw!r}"
        ) from error
    if not 0.0 <= value <= 1.0:
        raise NewsConfigError(
            f"news.sources[{index}].base_credibility 须在 0~1,收到 {value}"
        )
    return value


def _reject_duplicate_ids(sources: Sequence[NewsSourceConfig]) -> None:
    seen: set[str] = set()
    for item in sources:
        sid = item.source.source_id
        if sid in seen:
            raise NewsConfigError(f"news.sources 中 source_id 重复: {sid}")
        seen.add(sid)


def build_fetchers(config: NewsConfig) -> list[NewsFetcher]:
    """按配置装配采集器。引用未实现的采集器直接抛错,不静默跳过。"""
    fetchers: list[NewsFetcher] = []
    for item in config.sources:
        factory = FETCHER_REGISTRY.get(item.fetcher)
        if factory is None:
            raise NewsConfigError(
                f"来源 {item.source.source_id} 引用了未实现的采集器 {item.fetcher!r};"
                f"当前已实现: {sorted(FETCHER_REGISTRY) or '(无)'}。"
                "采集器需在完成来源合规核验后再注册。"
            )
        fetchers.append(factory(item.source, item.options))
    return fetchers


__all__ = [
    "FETCHER_REGISTRY",
    "NewsConfig",
    "NewsConfigError",
    "NewsSourceConfig",
    "build_fetchers",
    "load_news_config",
    "parse_close_cutoff",
]
