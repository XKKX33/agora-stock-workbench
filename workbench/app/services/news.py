"""舆情读服务。

把已入库的舆情原文与关联组装成页面可直接渲染的结构。这一层**只读**,
也不做任何网络请求:采集是盘后链条的事,打开页面不该触发抓取。

三种"没有舆情"必须分开报,页面才能说清到底缺在哪一环:
    no_source_registered —— 一个来源都没登记,采集链路没接
    never_collected      —— 登记了来源但从没采过
    no_news_on_date      —— 采过,但目标交易日当天没条目
返回空列表把这三件事糊成一件,用户只会看到"情绪页是空的"。
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository

# 页面单页最多展示的条目数。取值与 review 模块的 NEWS_HIGHLIGHT_LIMIT 无关:
# 这里是列表页,可以比复盘摘要多给一些。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class NewsService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def digest(
        self,
        trade_date: Optional[str] = None,
        *,
        include_duplicates: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """某交易日的舆情列表。

        trade_date 为 None 时取舆情库里最新的一天,而不是行情最新日:
        行情已更新但舆情还没采时,拿行情日期去查只会得到一个空列表,
        看起来像"当天没新闻"。
        """
        limit = self._clamp_limit(limit)
        sources = self.repository.news_sources()
        source_list = self._source_rows(sources)
        if sources.empty:
            return self._unavailable(
                "no_source_registered",
                "尚未登记任何舆情来源,采集链路未接入",
                trade_date=trade_date,
                sources=source_list,
            )

        earliest, latest = self.repository.news_date_range()
        coverage = {"earliest": earliest, "latest": latest}
        if earliest is None:
            return self._unavailable(
                "never_collected",
                f"已登记 {len(sources)} 个来源,但一条舆情都没采过",
                trade_date=trade_date,
                sources=source_list,
                coverage=coverage,
            )

        target = trade_date or latest
        items = self.repository.news_by_trade_date(
            str(target), include_duplicates=include_duplicates, limit=limit
        )
        if items.empty:
            return self._unavailable(
                "no_news_on_date",
                f"舆情库覆盖 {earliest}~{latest},但 {target} 当天没有条目",
                trade_date=target,
                sources=source_list,
                coverage=coverage,
            )

        return {
            "available": True,
            "trade_date": str(target),
            "missing_reason": None,
            "detail": None,
            "coverage": coverage,
            "include_duplicates": include_duplicates,
            "sources": source_list,
            "items": [_news_row(row) for _, row in items.iterrows()],
        }

    def for_stock(
        self,
        ts_code: str,
        *,
        as_of: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """某只股票关联到的舆情。as_of 非空时只看 <= as_of,保持前视纪律。"""
        return self._for_link("stock", ts_code, as_of=as_of, limit=limit)

    def for_industry(
        self,
        industry: str,
        *,
        as_of: Optional[str] = None,
        trade_date: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """某个行业关联到的舆情。

        trade_date 非空时只看指定交易日(舆情页按板块下钻);as_of 仍保留
        前视纪律语义,两者可同时使用。
        """
        return self._for_link(
            "industry", industry, as_of=as_of, trade_date=trade_date, limit=limit
        )

    def industry_overview(
        self,
        trade_date: Optional[str] = None,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """某交易日按行业板块聚合的舆情总览。

        与 digest 同一套"缺在哪一环"三态,页面才能说清是没接来源、没采过
        还是当天没有条目。可用时返回各板块的新闻数与情绪分布,并如实给出
        没有行业关联的条数(unlinked_count)——这些新闻不硬塞进任何板块。
        """
        limit = self._clamp_limit(limit)
        sources = self.repository.news_sources()
        source_list = self._source_rows(sources)
        if sources.empty:
            blocked = self._unavailable(
                "no_source_registered",
                "尚未登记任何舆情来源,采集链路未接入",
                trade_date=trade_date,
                sources=source_list,
            )
            blocked.update({"industries": [], "unlinked_count": None})
            return blocked

        earliest, latest = self.repository.news_date_range()
        coverage = {"earliest": earliest, "latest": latest}
        if earliest is None:
            blocked = self._unavailable(
                "never_collected",
                f"已登记 {len(sources)} 个来源,但一条舆情都没采过",
                trade_date=trade_date,
                sources=source_list,
                coverage=coverage,
            )
            blocked.update({"industries": [], "unlinked_count": None})
            return blocked

        target = str(trade_date or latest)
        if self.repository.news_by_trade_date(target, limit=1).empty:
            blocked = self._unavailable(
                "no_news_on_date",
                f"舆情库覆盖 {earliest}~{latest},但 {target} 当天没有条目",
                trade_date=target,
                sources=source_list,
                coverage=coverage,
            )
            blocked.update({"industries": [], "unlinked_count": None})
            return blocked

        summary = self.repository.news_industry_summary(target, limit=limit)
        return {
            "available": True,
            "trade_date": target,
            "missing_reason": None,
            "detail": None,
            "coverage": coverage,
            "sources": source_list,
            "industries": [
                {
                    "industry": _text(row.get("industry")),
                    "news_count": _count(row.get("news_count")),
                    "sentiment": {
                        "positive": _count(row.get("positive")),
                        "negative": _count(row.get("negative")),
                        "neutral": _count(row.get("neutral")),
                        "undecided": _count(row.get("undecided")),
                    },
                }
                for _, row in summary.iterrows()
            ],
            "unlinked_count": int(
                self.repository.news_unlinked_industry_count(target)
            ),
        }

    def sources(self) -> dict:
        """已登记来源清单,供页面展示"数据来自哪里"与合规审计。"""
        frame = self.repository.news_sources()
        return {
            "available": not frame.empty,
            "missing_reason": None if not frame.empty else "no_source_registered",
            "detail": None if not frame.empty else "尚未登记任何舆情来源",
            "items": self._source_rows(frame),
        }

    # -------------------------------------------------------------- 内部
    def _for_link(
        self,
        link_type: str,
        link_key: str,
        *,
        as_of: Optional[str],
        trade_date: Optional[str] = None,
        limit: int,
    ) -> dict:
        key = (link_key or "").strip()
        if not key:
            raise WorkbenchError(
                "invalid_link_key", "关联键不能为空", status_code=400
            )
        limit = self._clamp_limit(limit)
        if self.repository.news_sources().empty:
            return {
                "available": False,
                "link_type": link_type,
                "link_key": key,
                "as_of": as_of,
                "trade_date": trade_date,
                "missing_reason": "no_source_registered",
                "detail": "尚未登记任何舆情来源,采集链路未接入",
                "items": [],
            }
        frame = self.repository.news_for_link(
            link_type=link_type,
            link_key=key,
            as_of=as_of,
            trade_date=trade_date,
            limit=limit,
        )
        if frame.empty:
            detail = f"舆情库中没有与 {key} 关联的条目"
            if trade_date:
                detail += f"({trade_date})"
            return {
                "available": False,
                "link_type": link_type,
                "link_key": key,
                "as_of": as_of,
                "trade_date": trade_date,
                # 与"没采过"分开:采过但这只票/这个行业没关联上,是另一回事
                "missing_reason": "no_linked_news",
                "detail": detail,
                "items": [],
            }
        return {
            "available": True,
            "link_type": link_type,
            "link_key": key,
            "as_of": as_of,
            "trade_date": trade_date,
            "missing_reason": None,
            "detail": None,
            "items": [_linked_news_row(row) for _, row in frame.iterrows()],
        }

    @staticmethod
    def _clamp_limit(limit: int) -> int:
        return max(1, min(int(limit), MAX_LIMIT))

    @staticmethod
    def _unavailable(
        reason: str,
        detail: str,
        *,
        trade_date: Optional[str],
        sources: list[dict],
        coverage: Optional[dict] = None,
    ) -> dict:
        return {
            "available": False,
            "trade_date": trade_date,
            "missing_reason": reason,
            "detail": detail,
            "coverage": coverage or {"earliest": None, "latest": None},
            "sources": sources,
            "items": [],
        }

    @staticmethod
    def _source_rows(frame: pd.DataFrame) -> list[dict]:
        if frame.empty:
            return []
        return [
            {
                "source_id": _text(row.get("source_id")),
                "name": _text(row.get("name")),
                "kind": _text(row.get("kind")),
                "home_url": _text(row.get("home_url")),
                "base_credibility": _num(row.get("base_credibility")),
                # 合规备注要一路带到页面:哪个来源凭什么可以抓,要能当场查
                "compliance_note": _text(row.get("compliance_note")),
            }
            for _, row in frame.iterrows()
        ]


def _news_row(row: pd.Series) -> dict:
    """单条舆情。原文事实与判断分列,页面不必猜哪个字段能当结论。"""
    return {
        "news_id": _text(row.get("news_id")),
        "title": _text(row.get("title")),
        "summary": _text(row.get("summary")),
        "url": _text(row.get("url")),
        "published_at": _text(row.get("published_at")),
        "fetched_at": _text(row.get("fetched_at")),
        "trade_date": _text(row.get("trade_date")),
        "duplicate_of": _text(row.get("duplicate_of")),
        "source": {
            "source_id": _text(row.get("source_id")),
            "name": _text(row.get("source_name")),
            "kind": _text(row.get("source_kind")),
            "home_url": _text(row.get("source_home_url")),
            "base_credibility": _num(row.get("base_credibility")),
        },
        "judgement": {
            # 情绪与事件分类是规则推出的待验证判断,不是原文事实。
            # 值为 null 表示"未判定",不等于"中性":中性是有依据的结论。
            "label": "unverified",
            "event_type": _text(row.get("event_type")),
            "sentiment": _text(row.get("sentiment")),
            "sentiment_score": _num(row.get("sentiment_score")),
            "credibility": _num(row.get("credibility")),
        },
    }


def _linked_news_row(row: pd.Series) -> dict:
    """关联条目额外带上匹配依据——凭什么说这条新闻跟这只票有关,要能追溯。"""
    base = _news_row(row)
    base["link"] = {
        "match_basis": _text(row.get("match_basis")),
        "match_text": _text(row.get("match_text")),
        "confidence": _num(row.get("link_confidence")),
    }
    return base


def _text(value: Any) -> Optional[str]:
    """文本规整。NaN / 空串一律返回 None,页面据此显示"未判定"。

    只对 float 判 NaN,不用 pd.isna:后者遇到 list/ndarray 会返回逐元素数组,
    放进 if 会直接抛 ValueError。
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> Optional[float]:
    """数值规整。None / NaN / 非数一律返回 None——缺数据就是缺,不拿 0 顶替。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _count(value: Any) -> int:
    """计数规整。数据库 COUNT 不会是 NaN,但防御性处理:非数一律按 0 计。"""
    number = _num(value)
    return int(number) if number is not None else 0
