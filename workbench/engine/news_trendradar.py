"""TrendRadar 舆情采集器适配层。

把外部项目 TrendRadar(sansan0/TrendRadar, GPL-3.0)的热榜抓取能力接入本
工作台的舆情链路。设计要点与合规边界:

1. **GPL 隔离**:TrendRadar 是 GPL-3.0,原样保留在 `workbench/vendor/TrendRadar/`,
   不改一行、不拷贝其代码进本包。这里只用 `importlib` 按**单文件路径**加载它的
   `DataFetcher`(`trendradar/crawler/fetcher.py`,仅依赖 requests),刻意不
   `import trendradar`——那样会经包 __init__ 拽出 litellm/boto3 等重依赖,也会把
   GPL 代码更深地耦合进来。本文件是我们自己的代码,只是调用一个外部类。

2. **数据语义**:TrendRadar 经 newsnow 聚合 API 抓的是"全网热榜标题",每条只有
   标题、链接、热榜排名(ranks),**没有个股代码,也没有正文摘要**。因此:
   - summary 一律留 None(来源确实没给),不编造。
   - 个股/行业关联不在这里做,交给下游 `collect_news` 里既有的 `link_stocks`
     (靠标题匹配股票名/代码)完成。

3. **发布时间的诚实处理**:热榜条目**没有权威发布时间**,只有"此刻在榜"。本项目
   契约禁止用抓取时间冒充发布时间,但热榜的性质决定了唯一可得的时间就是采集时刻。
   处理办法是:published_at 记为采集时刻,并在 raw 里显式标注
   `time_basis="first_seen_at_collect"`,来源的 compliance_note 也写明这是热榜快照
   而非发布时间。下游据此按 close_cutoff 归属交易日。这是有标注的语义妥协,不是造假。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from .news import NewsFetcher, NewsSource, RawNewsItem

# 采集时刻相对发布时间的语义标注。落进每条 raw,审计时一眼可辨"这不是发布时间"。
TIME_BASIS = "first_seen_at_collect"

# vendor 内 DataFetcher 单文件路径,相对本文件定位(engine/ 与 vendor/ 同在 workbench/ 下)。
_FETCHER_PATH = (
    Path(__file__).resolve().parent.parent
    / "vendor"
    / "TrendRadar"
    / "trendradar"
    / "crawler"
    / "fetcher.py"
)


class TrendRadarConfigError(ValueError):
    """TrendRadar 采集器配置或环境非法。启动即暴露,不静默降级。"""


def _load_data_fetcher_class():
    """按单文件路径加载 vendor 的 DataFetcher,绕过 trendradar 包 __init__。

    找不到文件直接抛错:vendor 未克隆或路径变动是需要人处理的真实故障,
    静默返回 None 会让采集步伪装成"今天没消息"。
    """
    if not _FETCHER_PATH.is_file():
        raise TrendRadarConfigError(
            f"未找到 TrendRadar 采集器文件: {_FETCHER_PATH}。"
            "请确认已 `git clone https://github.com/sansan0/TrendRadar` 到 "
            "workbench/vendor/TrendRadar/(GPL-3.0,原样保留,勿改)。"
        )
    spec = importlib.util.spec_from_file_location("_trendradar_fetcher", _FETCHER_PATH)
    if spec is None or spec.loader is None:
        raise TrendRadarConfigError(f"无法为 {_FETCHER_PATH} 构造模块加载器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "DataFetcher"):
        raise TrendRadarConfigError(
            f"{_FETCHER_PATH} 中没有 DataFetcher 类,TrendRadar 版本可能已变更"
        )
    return module.DataFetcher


class TrendRadarFetcher:
    """把 TrendRadar 的热榜抓取包装成本工作台的 NewsFetcher。

    平台清单、API 地址、代理、请求间隔全部来自 options(在 settings.yaml 配),
    不写死——换源、加源、指向自建 newsnow 都只改配置,不动代码。
    """

    def __init__(self, source: NewsSource, options: dict):
        self._source = source
        self._platforms = _parse_platforms(options.get("platforms"))
        self._domain_rules = _parse_domain_rules(options.get("platforms"))
        self._api_url = _clean_str(options.get("api_url")) or None
        self._proxy_url = _clean_str(options.get("proxy_url")) or None
        self._request_interval = _parse_interval(options.get("request_interval_ms"))

    @property
    def source(self) -> NewsSource:
        return self._source

    def fetch(
        self, *, trade_date: str, window_start: datetime, window_end: datetime
    ) -> Sequence[RawNewsItem]:
        """抓取当前热榜快照并转成 RawNewsItem。

        热榜是"此刻在榜"的快照,没有历史时间轴,window_start/window_end 对它无意义
        (保留在签名里是为满足协议)。每条 published_at 记为采集时刻,归属交易日由
        下游按 close_cutoff 统一解析。抓取失败由 DataFetcher 内部重试后返回失败列表,
        这里对"全部平台都失败"显式抛错,不把空结果伪装成"今天没热点"。
        """
        data_fetcher_cls = _load_data_fetcher_class()
        fetcher = data_fetcher_cls(proxy_url=self._proxy_url, api_url=self._api_url)

        results, id_to_name, failed_ids = fetcher.crawl_websites(
            ids_list=list(self._platforms),
            request_interval=self._request_interval,
            domain_rules=self._domain_rules,
        )

        if not results and failed_ids:
            raise TrendRadarConfigError(
                f"TrendRadar 全部平台抓取失败: {failed_ids}。"
                f"当前 API: {self._api_url or '(默认 newsnow)'};"
                "请检查网络连通性或 api_url 配置,而非当作'今天没有热点'。"
            )

        collected_at = datetime.now().isoformat(timespec="seconds")
        items: list[RawNewsItem] = []
        for platform_id, titles in results.items():
            platform_name = id_to_name.get(platform_id, platform_id)
            for title, info in titles.items():
                url = _pick_url(info)
                if not url:
                    # 没有可追溯链接的条目丢弃:本项目要求每条舆情可回溯到原文。
                    continue
                items.append(
                    RawNewsItem(
                        source_id=self._source.source_id,
                        title=str(title).strip(),
                        url=url,
                        published_at=collected_at,
                        summary=None,  # 热榜无正文摘要,不编造
                        declared_codes=(),  # 热榜无结构化个股代码,关联交给下游 link_stocks
                        raw={
                            "platform_id": platform_id,
                            "platform_name": platform_name,
                            "ranks": list(info.get("ranks", [])),
                            "mobile_url": info.get("mobileUrl", "") or "",
                            "time_basis": TIME_BASIS,
                            "provider": "trendradar+newsnow",
                        },
                    )
                )
        return items


# ---------------------------------------------------------------- options 解析


def _parse_platforms(raw: Any) -> tuple[str, ...]:
    """平台清单必填且非空。空清单意味着采不到任何东西,属配置错误应显式报出。"""
    if not raw or not isinstance(raw, list):
        raise TrendRadarConfigError(
            "TrendRadar 来源缺少 options.platforms(热榜平台清单),"
            "示例见 config/settings.yaml"
        )
    ids: list[str] = []
    for index, entry in enumerate(raw):
        pid = _platform_id(entry, index)
        ids.append(pid)
    if not ids:
        raise TrendRadarConfigError("options.platforms 解析后为空")
    return tuple(ids)


def _parse_domain_rules(raw: Any) -> dict[str, str]:
    """从平台清单提取 {平台id: 预期域名},供 DataFetcher 做域名安全校验。

    没写 expected_domain 的平台不加入规则(DataFetcher 对空规则跳过校验)。
    """
    rules: dict[str, str] = {}
    if not isinstance(raw, list):
        return rules
    for index, entry in enumerate(raw):
        if isinstance(entry, dict):
            pid = _platform_id(entry, index)
            domain = _clean_str(entry.get("expected_domain"))
            if domain:
                rules[pid] = domain
    return rules


def _platform_id(entry: Any, index: int) -> str:
    """一个平台条目可以是字符串 id,或含 id 字段的映射。"""
    if isinstance(entry, str):
        pid = entry.strip()
    elif isinstance(entry, dict):
        pid = _clean_str(entry.get("id"))
    else:
        raise TrendRadarConfigError(
            f"options.platforms[{index}] 应为字符串或含 id 的映射,收到 {type(entry).__name__}"
        )
    if not pid:
        raise TrendRadarConfigError(f"options.platforms[{index}] 缺少平台 id")
    return pid


def _parse_interval(raw: Any) -> int:
    """请求间隔(毫秒)。留空用 TrendRadar 默认 100ms;非法值报错。

    小数(如 1.5)会被 int() 悄悄截断成 1——那正是本项目禁止的静默降级,
    故先挡掉带小数部分的浮点与布尔,让配置写错时启动即暴露。
    """
    if raw is None:
        return 100
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        raise TrendRadarConfigError(
            f"options.request_interval_ms 必须为整数毫秒,收到 {raw!r}"
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise TrendRadarConfigError(
            f"options.request_interval_ms 必须为整数毫秒,收到 {raw!r}"
        ) from error
    if value < 0:
        raise TrendRadarConfigError("options.request_interval_ms 不能为负")
    return value


def _pick_url(info: dict) -> str:
    """优先取正式链接,退回移动端链接。两者都空则返回空串(调用方丢弃)。"""
    url = _clean_str(info.get("url"))
    if url:
        return url
    return _clean_str(info.get("mobileUrl"))


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_trendradar_fetcher(source: NewsSource, options: dict) -> NewsFetcher:
    """FETCHER_REGISTRY 用的工厂。"""
    return TrendRadarFetcher(source, options)


__all__ = [
    "TIME_BASIS",
    "TrendRadarConfigError",
    "TrendRadarFetcher",
    "build_trendradar_fetcher",
]
