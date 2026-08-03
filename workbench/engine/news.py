"""舆情采集链路的编排层。

职责边界:本模块只负责"把采集器给的原始条目变成可入库、可追溯的记录",
文本判定全部委托给 `news_text`(纯函数,可离线穷举测试),具体的数据获取
全部委托给注入的采集器(`NewsFetcher` 协议)。三层分开的理由:

- 去重、时间边界、防未来数据泄漏这些规则必须能脱网测试;
- 换一个数据源不该动到去重与关联逻辑;
- 合规判断落在采集器与来源登记上,审计时只看 `news_sources.compliance_note` 即可。

不做的事:

1. **不静默跳过失败**。采集器抛错就整批中止并上抛;单条记录不合格(缺标题、
   发布时间解析不了)会被拒收,但拒收原因逐条记进结果里,由任务链展示,
   绝不悄悄丢弃。
2. **不编造字段**。来源没给摘要就留 NULL,判不出情绪就留 NULL。
3. **不重复写入**。news_id 是规范化链接的哈希,重复采集靠主键天然幂等;
   转载标 `duplicate_of` 而不是删行——原始数据一律保留。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Iterable, Optional, Protocol, Sequence

import pandas as pd

from .db import Store
from .news_text import (
    DEFAULT_CLOSE_CUTOFF,
    DEFAULT_HALF_LIFE_DAYS,
    NewsLink,
    NewsTextError,
    StockRef,
    classify_event,
    dedup_key_for,
    judge_sentiment,
    link_industries,
    link_stocks,
    news_id_for,
    parse_published_at,
    resolve_snapshot_trade_date,
    resolve_trade_date,
    score_credibility,
    time_decay,
    trade_date_to_datetime,
)

logger = logging.getLogger(__name__)

# 解析归属交易日时向前取的日历长度。覆盖长假(最长约 9 个自然日)后仍有余量,
# 保证"节前发布、节后归属"的公告能正确落到节后第一个交易日。
CALENDAR_LOOKBACK = 20


class NewsCollectError(RuntimeError):
    """采集链路的失败。一律上抛,由任务链记录为失败步骤并中止后续步骤。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class NewsSource:
    """一个已登记的舆情来源。字段与 news_sources 表一一对应。

    compliance_note 必填:它记录"凭什么可以采这个来源"(官方 API、公开 robots
    允许等)。没有这句话的来源不允许启用,事后也无从审计。
    """

    source_id: str
    name: str
    kind: str
    home_url: str
    base_credibility: Optional[float]
    compliance_note: str
    enabled: bool

    def __post_init__(self) -> None:
        if not self.source_id or not self.name:
            raise NewsCollectError("来源必须有 source_id 与展示名")
        if not self.compliance_note.strip():
            raise NewsCollectError(f"来源 {self.source_id} 缺少合规备注,拒绝登记")
        if self.kind not in ("notice", "news", "research"):
            raise NewsCollectError(f"来源 {self.source_id} 的类型非法: {self.kind}")

    def as_row(self) -> dict:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "kind": self.kind,
            "home_url": self.home_url,
            "base_credibility": self.base_credibility,
            "compliance_note": self.compliance_note,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class RawNewsItem:
    """采集器交出的原始条目。除 declared_codes 外都是来源原文,不做改写。"""

    source_id: str
    title: str
    url: str
    published_at: str
    summary: Optional[str] = None
    declared_codes: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


class NewsFetcher(Protocol):
    """采集器协议。实现者负责合规访问与网络容错,但不得吞掉失败。"""

    @property
    def source(self) -> NewsSource: ...

    def fetch(
        self, *, trade_date: str, window_start: datetime, window_end: datetime
    ) -> Sequence[RawNewsItem]: ...


@dataclass
class NewsCollectResult:
    """一次采集的结论。计数与拒收原因都要能原样展示在页面上。"""

    trade_date: str
    sources: list[str] = field(default_factory=list)
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    links: int = 0
    rejected: list[dict] = field(default_factory=list)
    by_trade_date: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "sources": list(self.sources),
            "fetched": self.fetched,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "links": self.links,
            "rejected": list(self.rejected),
            "rejected_count": len(self.rejected),
            "by_trade_date": dict(self.by_trade_date),
        }


# ---------------------------------------------------------------- 采集主流程


def collect_news(
    *,
    store: Store,
    trade_date: str,
    fetchers: Sequence[NewsFetcher],
    exchange: str = "SSE",
    close_cutoff: time = DEFAULT_CLOSE_CUTOFF,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> NewsCollectResult:
    """采集并入库某交易日的舆情。

    没有任何启用的采集器时返回空结果(fetched=0),由调用方标成"未配置来源",
    而不是在这里假装采到了 0 条正常新闻——两者在页面上必须能区分。
    """
    enabled = [f for f in fetchers if f.source.enabled]
    result = NewsCollectResult(trade_date=trade_date)
    _register_sources(store, fetchers)
    if not enabled:
        logger.warning("没有启用的舆情来源,%s 未采集", trade_date)
        return result

    open_dates = _calendar_window(store, exchange=exchange, trade_date=trade_date)
    universe = _load_universe(store, trade_date)
    window_start, window_end = _collect_window(
        store, exchange=exchange, trade_date=trade_date, close_cutoff=close_cutoff
    )

    raws: list[RawNewsItem] = []
    for fetcher in enabled:
        source_id = fetcher.source.source_id
        try:
            batch = fetcher.fetch(
                trade_date=trade_date, window_start=window_start, window_end=window_end
            )
        except Exception as error:  # noqa: BLE001 - 统一包装成采集失败并上抛
            raise NewsCollectError(
                f"来源 {source_id} 采集失败(窗口 {window_start.isoformat()} ~ "
                f"{window_end.isoformat()}): {type(error).__name__}: {error}"
            ) from error
        result.sources.append(source_id)
        raws.extend(batch)
    result.fetched = len(raws)
    if not raws:
        return result

    rows = _normalize_items(
        raws,
        open_dates=open_dates,
        close_cutoff=close_cutoff,
        half_life_days=half_life_days,
        credibility_by_source={f.source.source_id: f.source.base_credibility for f in enabled},
        result=result,
    )
    if not rows:
        return result

    _apply_dedup(store, rows, result)
    links = _build_links(rows, universe)
    _persist(store, rows, links, result)
    return result


def _register_sources(store: Store, fetchers: Iterable[NewsFetcher]) -> None:
    """登记全部来源(含未启用的)。未启用来源也要可见,便于解释"为什么没采"。"""
    rows = [f.source.as_row() for f in fetchers]
    if rows:
        store.upsert_news_sources(pd.DataFrame(rows))


def _calendar_window(store: Store, *, exchange: str, trade_date: str) -> list[str]:
    """归属交易日解析所需的日历片段:目标日往前若干开市日 + 其后一个开市日。"""
    dates = store.open_dates(exchange, trade_date, CALENDAR_LOOKBACK)
    if not dates:
        raise NewsCollectError(
            f"交易日历中找不到 {trade_date} 及之前的开市日({exchange}),请先更新 trade_cal"
        )
    nxt = store.sessions_after(exchange, trade_date, 1)
    return sorted(set(dates) | ({nxt} if nxt else set()))


def _collect_window(
    store: Store, *, exchange: str, trade_date: str, close_cutoff: time
) -> tuple[datetime, datetime]:
    """采集时间窗 = 上一开市日收盘 ~ 目标日收盘。正是归属到目标日的那段区间。"""
    previous = store.open_dates(exchange, trade_date, 2)
    start_date = previous[0] if len(previous) >= 2 else trade_date
    return (
        trade_date_to_datetime(start_date, close_cutoff),
        trade_date_to_datetime(trade_date, close_cutoff),
    )


def _load_universe(store: Store, trade_date: str) -> tuple[StockRef, ...]:
    """从当日截面构造关联用的股票档案。截面为空说明行情没落库,属于真实故障。"""
    snap = store.snapshot(trade_date)
    if snap.empty:
        raise NewsCollectError(
            f"{trade_date} 无行情截面,无法建立舆情与股票的关联;请先完成行情入库"
        )
    refs = []
    for row in snap.itertuples(index=False):
        name = getattr(row, "name", None)
        refs.append(
            StockRef(
                ts_code=str(row.ts_code),
                symbol=str(getattr(row, "symbol", "") or "")[:6],
                name=str(name) if isinstance(name, str) else "",
                industry=_clean(getattr(row, "industry", None)),
            )
        )
    return tuple(refs)


def _clean(value) -> Optional[str]:
    """把 NaN / 空串统一成 None。空串入库会让"缺失"看起来像"有值但为空"。"""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------- 归一化


def _normalize_items(
    raws: Sequence[RawNewsItem],
    *,
    open_dates: Sequence[str],
    close_cutoff: time,
    half_life_days: float,
    credibility_by_source: dict,
    result: NewsCollectResult,
) -> list[dict]:
    """逐条归一化。不合格的条目拒收并记录原因,合格的转成待入库行。"""
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in raws:
        try:
            row = _normalize_one(
                raw,
                open_dates=open_dates,
                close_cutoff=close_cutoff,
                half_life_days=half_life_days,
                base_credibility=credibility_by_source.get(raw.source_id),
            )
        except (NewsTextError, ValueError) as error:
            result.rejected.append(
                {
                    "source_id": raw.source_id,
                    "title": raw.title,
                    "url": raw.url,
                    "reason": str(error),
                }
            )
            logger.warning("舆情条目被拒收(%s): %s", raw.url, error)
            continue
        if row["news_id"] in seen:
            continue  # 同一批次里同一链接出现两次:主键相同,保留先出现的那条
        seen.add(row["news_id"])
        rows.append(row)
        result.by_trade_date[row["trade_date"]] = result.by_trade_date.get(row["trade_date"], 0) + 1
    rows.sort(key=lambda r: (r["published_at"], r["news_id"]))
    return rows


def _normalize_one(
    raw: RawNewsItem,
    *,
    open_dates: Sequence[str],
    close_cutoff: time,
    half_life_days: float,
    base_credibility: Optional[float],
) -> dict:
    """单条归一化。任何一步判不出就抛,由上层记成拒收原因,不产出半成品。"""
    title = (raw.title or "").strip()
    if not title:
        raise NewsTextError("缺少标题")
    published = parse_published_at(raw.published_at)
    time_basis = (raw.raw or {}).get("time_basis")
    if time_basis == "first_seen_at_collect":
        # 热榜快照没有权威发布时间,published_at 只是此刻在榜的采集时刻,
        # 归属最近已收盘交易日(见 resolve_snapshot_trade_date)。
        trade_date = resolve_snapshot_trade_date(published, open_dates=open_dates)
        decay = time_decay(
            published,
            as_of=trade_date_to_datetime(trade_date, close_cutoff),
            half_life_days=half_life_days,
            allow_future=True,
        )
    else:
        trade_date = resolve_trade_date(
            published, open_dates=open_dates, close_cutoff=close_cutoff
        )
        decay = time_decay(
            published,
            as_of=trade_date_to_datetime(trade_date, close_cutoff),
            half_life_days=half_life_days,
        )
    summary = _clean(raw.summary)
    judgement = judge_sentiment(title, summary)
    derived = {
        "sentiment": judgement.as_dict(),
        "decay_at_collect": round(decay, 6),
        "half_life_days": half_life_days,
        "declared_codes": list(raw.declared_codes),
        "time_basis": time_basis,
    }
    return {
        "news_id": news_id_for(raw.url),
        "source_id": raw.source_id,
        "title": title,
        "summary": summary,
        "url": raw.url.strip(),
        "published_at": published.isoformat(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "dedup_key": dedup_key_for(title, trade_date),
        "duplicate_of": None,
        "event_type": classify_event(title, summary),
        "sentiment": judgement.sentiment,
        "sentiment_score": judgement.score,
        "credibility": score_credibility(base=base_credibility, has_summary=summary is not None),
        "raw_json": json.dumps({"source": raw.raw, "derived": derived}, ensure_ascii=False),
        "_declared_codes": raw.declared_codes,
    }


# ---------------------------------------------------------------- 去重与关联


def _apply_dedup(store: Store, rows: list[dict], result: NewsCollectResult) -> None:
    """标记转载。按发布时间升序,首条为原始条目,其后同指纹的指向它。

    只标不删:被标为转载的条目仍然完整入库。哪几家转载了、什么时候转的,
    本身就是舆情热度的证据,删掉就再也拿不回来了。
    """
    originals = store.find_dedup_originals({r["dedup_key"] for r in rows})
    for row in rows:
        key = row["dedup_key"]
        first = originals.get(key)
        if first is None:
            originals[key] = row["news_id"]
            continue
        if first == row["news_id"]:
            continue
        row["duplicate_of"] = first
        result.duplicates += 1


def _build_links(rows: Sequence[dict], universe: Sequence[StockRef]) -> list[dict]:
    """为非转载条目建立关联。转载不重复关联,否则行业热度会被转载量放大。"""
    industry_of = {ref.ts_code: ref.industry for ref in universe}
    known = sorted({ref.industry for ref in universe if ref.industry})
    out: list[dict] = []
    for row in rows:
        if row["duplicate_of"]:
            continue
        stock_links = link_stocks(
            title=row["title"],
            summary=row["summary"],
            universe=universe,
            declared_codes=row["_declared_codes"],
        )
        industry_links = link_industries(
            title=row["title"],
            summary=row["summary"],
            stock_links=stock_links,
            industry_of=industry_of,
            known_industries=known,
        )
        out.extend(_link_row(row["news_id"], link) for link in (*stock_links, *industry_links))
    return out


def _link_row(news_id: str, link: NewsLink) -> dict:
    row = link.as_dict()
    row["news_id"] = news_id
    return row


def _persist(
    store: Store, rows: list[dict], links: list[dict], result: NewsCollectResult
) -> None:
    """落库。内部字段(下划线开头)在此剥离,不进表。"""
    frame = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    result.stored = store.upsert_news_items(frame)
    if links:
        result.links = store.upsert_news_links(pd.DataFrame(links))


__all__ = [
    "CALENDAR_LOOKBACK",
    "NewsCollectError",
    "NewsCollectResult",
    "NewsFetcher",
    "NewsSource",
    "RawNewsItem",
    "collect_news",
]
