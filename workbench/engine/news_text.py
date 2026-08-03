"""舆情文本处理的纯函数层。

这一层刻意不碰数据库、不碰网络,全部是"给定输入就得到确定输出"的纯函数。
理由:去重、时间边界、防未来数据泄漏这几条最要命的规则,只有在能用固定时钟
和固定样本反复验证时才谈得上可靠;一旦掺进 IO,测试就只能测个大概。

三条不可退让的纪律:

1. **判不出就返回 None**。情绪判不出返回 None 而不是 0,事件分不出返回 None
   而不是"其他"。页面上"未判定"和"中性"是两种完全不同的信息,合成一种就是编造。
2. **关联必须有依据**。每条股票/行业关联都带 match_basis 与命中的原文片段;
   说不出"为什么这条新闻和这只股票有关",就不产出这条关联。
3. **归属交易日按收盘时点切分**。收盘后发布的新闻归到下一个交易日——它没有参与
   当日的价格形成,算进当日会让"舆情解释了走势"变成事后诸葛。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------- 常量

# 链接里对"是不是同一篇文章"毫无贡献的跟踪参数。不剔掉它们,同一篇文章
# 从不同入口进来就会生成两个 news_id,去重从第一步就失效。
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "from",
        "spm",
        "share_token",
        "share_from",
        "ref",
        "referrer",
    }
)

# 指纹长度:sha256 取前 32 位十六进制。128 bit 的碰撞概率对本场景的数据量
# (每日数千条)完全够用,同时比全长好读。
_HASH_WIDTH = 32

# A 股收盘时点。归属交易日以此切分,可由调用方覆盖。
DEFAULT_CLOSE_CUTOFF = time(15, 0)

# 情绪分档阈值。|score| 小于它就判中性(注意:这是"有证据但正负相抵"的中性,
# 与"没有任何证据"的 None 是两回事)。
NEUTRAL_BAND = 0.15

# 时间衰减默认半衰期(自然日)。三天前的消息影响力减半,符合 A 股题材节奏。
DEFAULT_HALF_LIFE_DAYS = 3.0

# 股票名匹配的最小长度。两字名(如"中兴")在正文里误命中的概率太高,
# 只靠名字不足以支撑一条关联;它们仍可通过代码匹配被正确关联。
MIN_NAME_MATCH_LEN = 3

# 摘要缺失时对可信度的折损。缺正文摘要意味着研判只能靠标题,可信度应当降低,
# 但不至于归零——标题本身仍是来源给出的事实。
_MISSING_SUMMARY_PENALTY = 0.1

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 规范化标题时剔除的标点与空白。转载常在标题上加书名号、感叹号或空格,
# 这些差异不构成"另一篇新闻"。
_PUNCT_RE = re.compile(r"[\s　·,,。.、;;::!!??\"'“”‘’()()\[\]【】《》<>|/\\-—_~]+")
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 编辑标记:转载/首发之类只标明"谁在转",不改变新闻本身,规范化时剔除,
# 否则 "【转载】隆基绿能业绩预告" 与原文会被判成两条。只收录明确的多字转载/
# 来源标记,不含单字(如裸"转"会误伤"可转债""转型")、也不含任何新闻内容词,
# 以免把真正不同的标题错并成一条。
_EDITORIAL_MARKERS = ("转载", "转发", "独家", "首发", "原创")
_EDITORIAL_RE = re.compile("|".join(_EDITORIAL_MARKERS))


class NewsTextError(ValueError):
    """文本层的输入非法。调用方应当上抛,不得吞掉后继续。"""


# ---------------------------------------------------------------- 规范化与指纹


def normalize_url(url: str) -> str:
    """规范化链接:去锚点、剔跟踪参数、小写主机名、去末尾斜杠。

    只做无损规范化。查询参数里除跟踪参数外一律保留并排序——很多站点的
    文章 id 就在查询参数里,擅自丢弃会把不同文章合并成一条。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if not url or not url.strip():
        raise NewsTextError("空链接无法规范化")
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_WIDTH]


def news_id_for(url: str) -> str:
    """条目主键 = 规范化链接的哈希。同一链接重复采集天然幂等。"""
    return _digest(normalize_url(url))


def news_id_for_content(*, source_id: str, published_at: str, title: str) -> str:
    """无原文链接来源的主键 = 来源 + 发布时间 + 规范化标题的哈希。

    部分官方接口(如资讯快讯类)只给标题与正文,不给逐条链接。这种情况下
    主键退回内容指纹,同时 url 留 NULL 让页面显示"来源未提供原文链接"——
    绝不拼一个猜出来的链接冒充可追溯。
    """
    if not source_id or not published_at:
        raise NewsTextError("无链接条目必须同时提供来源与发布时间")
    return _digest(f"{source_id}|{published_at}|{normalize_title(title)}")


def normalize_title(title: str) -> str:
    """标题规范化:去标点空白、全角数字转半角、剔转载标记、统一小写。

    转载/首发之类的编辑标记不改变新闻本身,连同书名号一并剔除,好让同一条
    热榜标题在不同平台的转发被判成同一条(热榜跨平台转发是常态)。
    """
    if title is None:
        raise NewsTextError("标题为空,无法生成去重指纹")
    text = str(title).translate(_FULLWIDTH_DIGITS).lower()
    text = _PUNCT_RE.sub("", text)
    text = _EDITORIAL_RE.sub("", text)
    if not text:
        raise NewsTextError(f"标题规范化后为空: {title!r}")
    return text


def dedup_key_for(title: str, trade_date: str) -> str:
    """去重指纹 = 归属交易日 + 规范化标题的哈希。

    带上交易日是刻意的:"XX公司发布股东减持公告"这类标题每隔几周就会重现,
    不按日切分会把几个月前的旧闻误判成今天这条的转载。
    """
    if not trade_date:
        raise NewsTextError("缺少归属交易日,无法生成去重指纹")
    return _digest(f"{trade_date}|{normalize_title(title)}")


# ---------------------------------------------------------------- 时间


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)


def parse_published_at(raw: str) -> datetime:
    """解析来源给出的发布时间。解析不了就抛,绝不用当前时间顶替。

    用抓取时间冒充发布时间会直接摧毁时间边界:一条三天前的旧闻会被当成
    今天的新消息计入情绪,而且永远查不出来。
    """
    if raw is None or not str(raw).strip():
        raise NewsTextError("来源未提供发布时间")
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise NewsTextError(f"无法解析发布时间: {raw!r}")


def resolve_trade_date(
    published_at: datetime,
    *,
    open_dates: Sequence[str],
    close_cutoff: time = DEFAULT_CLOSE_CUTOFF,
) -> str:
    """把发布时间映射到归属交易日。

    规则:开市日收盘时点(含)之前发布 -> 当日;之后发布、或发布日非开市日
    -> 其后第一个开市日。

    日历没覆盖到时抛错而不是猜一个日期:交易日历过期是需要人处理的真实故障,
    静默地把新闻挂到最后一个已知交易日,会让整批舆情落到错误的批次里。
    """
    if not open_dates:
        raise NewsTextError("交易日历为空,无法确定归属交易日")
    day = published_at.strftime("%Y%m%d")
    ordered = sorted(open_dates)
    if day in set(ordered) and published_at.time() <= close_cutoff:
        return day
    for candidate in ordered:
        if candidate > day:
            return candidate
    raise NewsTextError(
        f"交易日历未覆盖发布时间 {published_at.isoformat()}(日历止于 {ordered[-1]}),"
        "请先更新 trade_cal 再采集"
    )


def resolve_snapshot_trade_date(
    published_at: datetime,
    *,
    open_dates: Sequence[str],
) -> str:
    """热榜快照条目的归属交易日。

    快照来源(如 TrendRadar 热榜)没有权威发布时间,published_at 只是此刻在榜
    的采集时刻,归属规则与普通发布不同:

    - 采集当天在日历中 -> 当天(无论是否已过收盘:盘后采的热榜就是当天的热点);
    - 采集当天不在日历中(周末/节假日) -> 之前最近的开市日;
    - 采集时刻晚于日历最后一天(日历未覆盖未来) -> 日历最后一天,
      热榜快照不可能属于一个还没发生的交易日,也不该因此整批拒收。

    日历为空时抛错,与 resolve_trade_date 一致:这是需要人处理的真实故障。
    """
    if not open_dates:
        raise NewsTextError("交易日历为空,无法确定归属交易日")
    ordered = sorted(open_dates)
    day = published_at.strftime("%Y%m%d")
    if day in ordered:
        return day
    for candidate in reversed(ordered):
        if candidate < day:
            return candidate
    # 采集时刻早于日历全部日期:时钟错位的极端情况,归最早一天,不猜日历外日期。
    return ordered[0]

def time_decay(
    published_at: datetime,
    *,
    as_of: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    allow_future: bool = False,
) -> float:
    """按半衰期计算时间衰减权重,返回 (0, 1]。

    published_at 晚于 as_of 时直接抛错:那是未来数据泄漏,不是可以四舍五入的
    边界情况。复盘 T 日却用到了 T+1 的消息,任何结论都不再可信。
    """
    if half_life_days <= 0:
        raise NewsTextError(f"半衰期必须为正: {half_life_days}")
    elapsed = (as_of - published_at).total_seconds() / 86400.0
    if elapsed < 0:
        if not allow_future:
            raise NewsTextError(
                f"发布时间 {published_at.isoformat()} 晚于基准时点 {as_of.isoformat()},"
                "属于未来数据,拒绝计算权重"
            )
        # 快照来源没有权威发布时间,采集时刻晚于批次收盘是常态:
        # 权重按批次时点信息已在榜计(decay=1),不猜衰减曲线。
        elapsed = 0.0
    return math.pow(0.5, elapsed / half_life_days)


# ---------------------------------------------------------------- 事件分类

# 按优先级排列:越靠前的类别越"确定"。一条同时提到处罚与业绩的公告,
# 判成"监管处罚"比判成"业绩"更贴近它的实际影响。
EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("监管处罚", ("立案", "处罚", "警示函", "问询函", "关注函", "违规", "风险警示", "退市")),
    ("停复牌", ("停牌", "复牌")),
    ("并购重组", ("重组", "并购", "收购", "资产注入", "借壳", "换股")),
    ("业绩", ("业绩预告", "业绩快报", "预增", "预减", "扭亏", "年报", "半年报", "季报", "净利润")),
    ("订单合同", ("中标", "签约", "订单", "框架协议", "供货协议")),
    ("股东行为", ("减持", "增持", "回购", "股权转让", "质押")),
    ("融资", ("定增", "可转债", "配股", "募集资金", "发行股份", "ipo")),
    ("分红", ("分红", "派息", "送转", "利润分配")),
    ("人事", ("辞职", "聘任", "换届", "董事长", "总经理")),
    ("政策", ("政策", "规划", "补贴", "发改委", "工信部", "国务院", "监管部门")),
    ("交易异动", ("涨停", "跌停", "龙虎榜", "异动", "停盘核查")),
)


def classify_event(title: str, summary: Optional[str] = None) -> Optional[str]:
    """事件分类。命中不了任何规则返回 None,由上层显示"未分类"。

    不设"其他"兜底类:把没看懂的东西装进一个类别里,统计出来的"事件分布"
    就成了规则覆盖率的镜像,而不是市场的真实结构。
    """
    text = _search_text(title, summary)
    for event_type, keywords in EVENT_RULES:
        if any(word in text for word in keywords):
            return event_type
    return None


def _search_text(title: str, summary: Optional[str]) -> str:
    parts = [str(title or "")]
    if summary:
        parts.append(str(summary))
    return " ".join(parts).lower()


# ---------------------------------------------------------------- 情绪

POSITIVE_WORDS: tuple[str, ...] = (
    "预增", "扭亏", "超预期", "大涨", "涨停", "新高", "中标", "签约", "增持", "回购",
    "获批", "突破", "提价", "满产", "利好", "创纪录", "增长", "达产", "投产", "合作",
)
NEGATIVE_WORDS: tuple[str, ...] = (
    "预减", "亏损", "下滑", "跌停", "新低", "减持", "处罚", "立案", "问询", "违规",
    "退市", "减值", "下调", "终止", "诉讼", "停产", "风险警示", "利空", "爽约", "违约",
)


@dataclass(frozen=True)
class SentimentJudgement:
    """情绪判定结果。evidence 保留命中的词,让"为什么判成利好"可追溯。"""

    sentiment: Optional[str]
    score: Optional[float]
    positive_hits: tuple[str, ...]
    negative_hits: tuple[str, ...]

    @property
    def evidence(self) -> tuple[str, ...]:
        return self.positive_hits + self.negative_hits

    def as_dict(self) -> dict:
        return {
            "sentiment": self.sentiment,
            "sentiment_score": self.score,
            "positive_hits": list(self.positive_hits),
            "negative_hits": list(self.negative_hits),
        }


def judge_sentiment(title: str, summary: Optional[str] = None) -> SentimentJudgement:
    """基于关键词的情绪方向判定。

    两种"中性"必须区分开:
    - 命中了正负两类词且相抵 -> sentiment="neutral", score≈0,这是有证据的判断;
    - 一个词都没命中 -> sentiment=None, score=None,这是"判不出"。
    后者绝不填 0,否则页面上一片"中性"会被误读成"市场情绪平稳"。
    """
    text = _search_text(title, summary)
    pos = tuple(w for w in POSITIVE_WORDS if w in text)
    neg = tuple(w for w in NEGATIVE_WORDS if w in text)
    if not pos and not neg:
        return SentimentJudgement(None, None, (), ())
    score = (len(pos) - len(neg)) / (len(pos) + len(neg))
    if score > NEUTRAL_BAND:
        label = "positive"
    elif score < -NEUTRAL_BAND:
        label = "negative"
    else:
        label = "neutral"
    return SentimentJudgement(label, round(score, 4), pos, neg)


# ---------------------------------------------------------------- 可信度


def score_credibility(
    *,
    base: Optional[float],
    has_summary: bool,
) -> Optional[float]:
    """综合可信度 = 来源基准可信度 × 正文完整度折损。

    来源没有登记基准可信度时返回 None,而不是给一个默认值:凭空给出的 0.5
    会在页面上显示成"中等可信",看起来像是评估过,其实什么都没评估。
    """
    if base is None:
        return None
    if not 0.0 <= base <= 1.0:
        raise NewsTextError(f"来源基准可信度须在 0~1: {base}")
    value = base - (0.0 if has_summary else _MISSING_SUMMARY_PENALTY)
    return round(max(0.0, min(1.0, value)), 4)


# ---------------------------------------------------------------- 关联


@dataclass(frozen=True)
class NewsLink:
    """一条舆情与股票/行业的关联。match_basis 与 match_text 都必填。"""

    link_type: str
    link_key: str
    match_basis: str
    match_text: str
    confidence: float

    def as_dict(self) -> dict:
        return {
            "link_type": self.link_type,
            "link_key": self.link_key,
            "match_basis": self.match_basis,
            "match_text": self.match_text,
            "confidence": self.confidence,
        }


# 各类匹配依据的置信度。来源直接给出的代码最可信,正文里出现的名字最弱。
CONFIDENCE_BY_BASIS = {
    "source_field": 1.0,
    "code_in_text": 0.95,
    "name_in_text": 0.75,
    "via_linked_stock": 0.6,
    "industry_name_in_text": 0.7,
}


@dataclass(frozen=True)
class StockRef:
    """关联匹配用的股票档案。由调用方从 stock_basic 快照构造。"""

    ts_code: str
    symbol: str
    name: str
    industry: Optional[str]


def link_stocks(
    *,
    title: str,
    summary: Optional[str],
    universe: Iterable[StockRef],
    declared_codes: Sequence[str] = (),
) -> tuple[NewsLink, ...]:
    """把一条舆情关联到具体股票。没有依据的关联一律不产出。

    declared_codes 是来源结构化字段里直接给出的代码(如公告接口的 ts_code),
    它的可信度高于任何正文匹配,因此优先级最高且不会被正文匹配覆盖。
    """
    text = _search_text(title, summary)
    declared = {str(c).strip() for c in declared_codes if str(c).strip()}
    codes_in_text = set(_CODE_RE.findall(text))
    links: dict[str, NewsLink] = {}
    for ref in universe:
        basis, matched = _match_stock(ref, declared, codes_in_text, text)
        if basis is None:
            continue
        links[ref.ts_code] = NewsLink(
            link_type="stock",
            link_key=ref.ts_code,
            match_basis=basis,
            match_text=matched,
            confidence=CONFIDENCE_BY_BASIS[basis],
        )
    return tuple(links[k] for k in sorted(links))


def _match_stock(
    ref: StockRef,
    declared: set[str],
    codes_in_text: set[str],
    text: str,
) -> tuple[Optional[str], str]:
    """单票匹配。返回 (match_basis, 命中片段);没命中返回 (None, "")。"""
    if ref.ts_code in declared or ref.symbol in declared:
        return "source_field", ref.ts_code
    if ref.symbol and ref.symbol in codes_in_text:
        return "code_in_text", ref.symbol
    name = (ref.name or "").strip()
    if len(name) >= MIN_NAME_MATCH_LEN and name.lower() in text:
        return "name_in_text", name
    return None, ""


def link_industries(
    *,
    title: str,
    summary: Optional[str],
    stock_links: Sequence[NewsLink],
    industry_of: dict[str, Optional[str]],
    known_industries: Iterable[str] = (),
) -> tuple[NewsLink, ...]:
    """行业关联:正文直接点名的行业,以及已关联个股所属的行业。

    两种依据分开记录。"新闻提到了光伏"和"新闻提到的这只股票属于光伏"是强弱
    不同的证据,混成一种会让行业热度统计失去分辨力。
    """
    text = _search_text(title, summary)
    links: dict[str, NewsLink] = {}
    for industry in known_industries:
        name = (industry or "").strip()
        if len(name) >= MIN_NAME_MATCH_LEN and name.lower() in text:
            links[name] = NewsLink(
                link_type="industry",
                link_key=name,
                match_basis="industry_name_in_text",
                match_text=name,
                confidence=CONFIDENCE_BY_BASIS["industry_name_in_text"],
            )
    for link in stock_links:
        industry = (industry_of.get(link.link_key) or "").strip()
        if not industry or industry in links:
            continue
        links[industry] = NewsLink(
            link_type="industry",
            link_key=industry,
            match_basis="via_linked_stock",
            match_text=link.link_key,
            confidence=round(CONFIDENCE_BY_BASIS["via_linked_stock"] * link.confidence, 4),
        )
    return tuple(links[k] for k in sorted(links))


def trade_date_to_datetime(trade_date: str, at: time = DEFAULT_CLOSE_CUTOFF) -> datetime:
    """把 YYYYMMDD 交易日转成该日某时点的 datetime,供时间衰减取基准。"""
    try:
        day = date(int(trade_date[0:4]), int(trade_date[4:6]), int(trade_date[6:8]))
    except (TypeError, ValueError, IndexError) as error:
        raise NewsTextError(f"非法交易日: {trade_date!r}") from error
    return datetime.combine(day, at)


__all__ = [
    "CONFIDENCE_BY_BASIS",
    "DEFAULT_CLOSE_CUTOFF",
    "DEFAULT_HALF_LIFE_DAYS",
    "EVENT_RULES",
    "NEGATIVE_WORDS",
    "NEUTRAL_BAND",
    "NewsLink",
    "NewsTextError",
    "POSITIVE_WORDS",
    "SentimentJudgement",
    "StockRef",
    "classify_event",
    "dedup_key_for",
    "judge_sentiment",
    "link_industries",
    "link_stocks",
    "news_id_for",
    "news_id_for_content",
    "normalize_title",
    "normalize_url",
    "parse_published_at",
    "resolve_trade_date",
    "score_credibility",
    "time_decay",
    "trade_date_to_datetime",
]
