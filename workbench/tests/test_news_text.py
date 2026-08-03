"""舆情纯函数层单测:去重、时间边界、防未来数据泄漏、股票/行业关联。

这一层不碰数据库、不碰网络,时钟与交易日历都由参数注入,因此可以离线穷举。
第一阶段最容易悄悄退化的几条规则全部锁在这里:

- 同一篇文章换个跟踪参数不能变成两条(news_id 幂等)。
- 收盘后发的消息不能挂到当天(时间边界)。
- 用到晚于基准时点的消息必须抛错,不能当成 0 衰减(未来数据泄漏)。
- 判不出情绪要给 None,不能给 0——"证据显示中性"和"没法判断"是两件事。
- 没有匹配依据的关联一条都不许产出。

运行:
    cd workbench
    python -m pytest tests/test_news_text.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.news_text import (  # noqa: E402
    MIN_NAME_MATCH_LEN,
    NewsTextError,
    StockRef,
    classify_event,
    dedup_key_for,
    judge_sentiment,
    link_industries,
    link_stocks,
    news_id_for,
    news_id_for_content,
    normalize_title,
    normalize_url,
    parse_published_at,
    resolve_snapshot_trade_date,
    resolve_trade_date,
    score_credibility,
    time_decay,
    trade_date_to_datetime,
)

pytestmark = pytest.mark.unit

# 连续三个开市日,中间的 20260731 是周五,20260803 是下周一
OPEN_DATES = ("20260729", "20260730", "20260731", "20260803")


# ---------------------------------------------------------------- URL 与指纹


def test_normalize_url_strips_tracking_and_fragment():
    """跟踪参数与锚点属于同一篇文章的不同入口,必须归一。"""
    url = "HTTPS://News.Example.com/a/123?utm_source=wx&spm=x&id=9#comment"
    assert normalize_url(url) == "https://news.example.com/a/123?id=9"


def test_normalize_url_keeps_non_tracking_query():
    """很多站点的文章 id 就在查询参数里,擅自丢弃会把不同文章合并成一条。"""
    first = normalize_url("https://e.com/show?docid=1&cat=a")
    second = normalize_url("https://e.com/show?docid=2&cat=a")
    assert first != second
    # 参数顺序不影响结果
    assert normalize_url("https://e.com/show?cat=a&docid=1") == first


def test_normalize_url_drops_trailing_slash():
    assert normalize_url("https://e.com/a/") == normalize_url("https://e.com/a")


def test_normalize_url_rejects_empty():
    with pytest.raises(NewsTextError):
        normalize_url("   ")


def test_news_id_is_idempotent_across_tracking_variants():
    """重复采集同一篇文章必须落到同一个主键上,否则每天都会灌进重复行。"""
    base = "https://news.example.com/a/123"
    assert news_id_for(base) == news_id_for(base + "?utm_campaign=push")
    assert news_id_for(base) == news_id_for(base + "#p2")
    assert len(news_id_for(base)) == 32


def test_news_id_differs_for_different_articles():
    assert news_id_for("https://e.com/a") != news_id_for("https://e.com/b")


def test_news_id_for_content_requires_source_and_time():
    """无链接来源退回内容指纹,但来源与发布时间缺一不可。"""
    ok = news_id_for_content(
        source_id="demo", published_at="2026-07-31T10:00:00", title="某公司预增"
    )
    assert len(ok) == 32
    with pytest.raises(NewsTextError):
        news_id_for_content(source_id="", published_at="2026-07-31", title="标题")
    with pytest.raises(NewsTextError):
        news_id_for_content(source_id="demo", published_at="", title="标题")


def test_normalize_title_folds_punctuation_and_fullwidth_digits():
    assert normalize_title("【公告】某公司　业绩预增 １２３%") == normalize_title(
        "公告:某公司业绩预增123%"
    )


def test_normalize_title_rejects_punctuation_only():
    with pytest.raises(NewsTextError):
        normalize_title("——【】")


def test_dedup_key_groups_reprints_within_a_day():
    """同一天不同站点的转载归成一组;跨天的同名标题不合并。"""
    a = dedup_key_for("某公司发布业绩预告", "20260731")
    b = dedup_key_for("【转载】某公司发布业绩预告!", "20260731")
    c = dedup_key_for("某公司发布业绩预告", "20260803")
    assert a == b
    assert a != c


# ---------------------------------------------------------------- 时间边界


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-07-31 09:30:00", datetime(2026, 7, 31, 9, 30)),
        ("2026-07-31 09:30", datetime(2026, 7, 31, 9, 30)),
        ("2026/07/31 09:30:00", datetime(2026, 7, 31, 9, 30)),
        ("20260731093000", datetime(2026, 7, 31, 9, 30)),
        ("20260731", datetime(2026, 7, 31, 0, 0)),
        ("2026-07-31T09:30:00", datetime(2026, 7, 31, 9, 30)),
    ],
)
def test_parse_published_at_accepts_common_formats(raw, expected):
    assert parse_published_at(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "昨天", "2026-13-45"])
def test_parse_published_at_refuses_to_guess(raw):
    """解析不了就抛。用抓取时间顶替会让旧闻冒充今天的消息,且永远查不出来。"""
    with pytest.raises(NewsTextError):
        parse_published_at(raw)


def test_resolve_trade_date_before_close_is_same_day():
    got = resolve_trade_date(datetime(2026, 7, 31, 14, 59), open_dates=OPEN_DATES)
    assert got == "20260731"


def test_resolve_trade_date_at_close_is_same_day():
    """收盘时点含在当日内,15:00:00 整仍算当天。"""
    got = resolve_trade_date(datetime(2026, 7, 31, 15, 0, 0), open_dates=OPEN_DATES)
    assert got == "20260731"


def test_resolve_trade_date_after_close_rolls_forward():
    """收盘后的消息不能用来解释当天的涨跌,必须挂到下一个开市日。"""
    got = resolve_trade_date(datetime(2026, 7, 31, 15, 0, 1), open_dates=OPEN_DATES)
    assert got == "20260803"


def test_resolve_trade_date_on_closed_day_rolls_forward():
    """周六(20260801)非开市日,不论什么时间都归到下一个开市日。"""
    got = resolve_trade_date(datetime(2026, 8, 1, 9, 0), open_dates=OPEN_DATES)
    assert got == "20260803"


def test_resolve_trade_date_respects_custom_cutoff():
    got = resolve_trade_date(
        datetime(2026, 7, 31, 11, 40),
        open_dates=OPEN_DATES,
        close_cutoff=time(11, 30),
    )
    assert got == "20260803"


def test_resolve_trade_date_raises_when_calendar_exhausted():
    """日历过期是需要人处理的故障,不能静默挂到最后一个已知交易日。"""
    with pytest.raises(NewsTextError) as excinfo:
        resolve_trade_date(datetime(2026, 9, 1, 10, 0), open_dates=OPEN_DATES)
    assert "trade_cal" in str(excinfo.value)


def test_resolve_trade_date_rejects_empty_calendar():
    with pytest.raises(NewsTextError):
        resolve_trade_date(datetime(2026, 7, 31, 10, 0), open_dates=())


# ---------------------------------------------------------------- 热榜快照归属


def test_snapshot_on_open_day_stays_same_day():
    """盘中/盘后采的热榜都归当天:此刻在榜就是当天的热点。"""
    at = datetime(2026, 7, 31, 17, 37)
    assert resolve_snapshot_trade_date(at, open_dates=OPEN_DATES) == "20260731"


def test_snapshot_on_closed_day_goes_back():
    """周末采集的热榜归最近一个已收盘交易日。"""
    at = datetime(2026, 8, 1, 17, 37)
    assert resolve_snapshot_trade_date(at, open_dates=OPEN_DATES) == "20260731"


def test_snapshot_after_calendar_end_uses_last_open_day():
    """日历未覆盖未来时归日历最后一天,不整批拒收。"""
    at = datetime(2026, 8, 1, 17, 37)
    assert (
        resolve_snapshot_trade_date(
            at, open_dates=("20260729", "20260730", "20260731")
        )
        == "20260731"
    )


def test_snapshot_rejects_empty_calendar():
    with pytest.raises(NewsTextError):
        resolve_snapshot_trade_date(datetime(2026, 7, 31, 10, 0), open_dates=())


def test_time_decay_allow_future_clamps_to_zero():
    """快照条目允许采集时刻晚于批次时点,权重钳到 1,不猜衰减。"""
    decay = time_decay(
        datetime(2026, 8, 1, 17, 37),
        as_of=trade_date_to_datetime("20260731"),
        allow_future=True,
    )
    assert decay == 1.0


def test_time_decay_still_rejects_future_by_default():
    """普通来源默认仍拒绝未来数据,防线不放松。"""
    with pytest.raises(NewsTextError):
        time_decay(
            datetime(2026, 8, 1, 17, 37),
            as_of=trade_date_to_datetime("20260731"),
        )

def test_time_decay_is_one_at_zero_elapsed():
    now = datetime(2026, 7, 31, 15, 0)
    assert time_decay(now, as_of=now) == pytest.approx(1.0)


def test_time_decay_halves_after_one_half_life():
    got = time_decay(
        datetime(2026, 7, 28, 15, 0),
        as_of=datetime(2026, 7, 31, 15, 0),
        half_life_days=3.0,
    )
    assert got == pytest.approx(0.5)


def test_time_decay_is_monotonically_decreasing():
    as_of = datetime(2026, 7, 31, 15, 0)
    fresh = time_decay(datetime(2026, 7, 31, 9, 0), as_of=as_of)
    stale = time_decay(datetime(2026, 7, 20, 9, 0), as_of=as_of)
    assert 0 < stale < fresh <= 1.0


def test_time_decay_rejects_future_news():
    """T 日复盘用到 T+1 的消息就是未来数据泄漏,不是可以四舍五入的边界。"""
    with pytest.raises(NewsTextError) as excinfo:
        time_decay(
            datetime(2026, 8, 3, 9, 30),
            as_of=datetime(2026, 7, 31, 15, 0),
        )
    assert "未来数据" in str(excinfo.value)


def test_time_decay_rejects_non_positive_half_life():
    now = datetime(2026, 7, 31, 15, 0)
    with pytest.raises(NewsTextError):
        time_decay(now, as_of=now, half_life_days=0)


def test_trade_date_to_datetime_uses_close_cutoff():
    assert trade_date_to_datetime("20260731") == datetime(2026, 7, 31, 15, 0)


def test_trade_date_to_datetime_rejects_bad_date():
    with pytest.raises(NewsTextError):
        trade_date_to_datetime("2026-07-31")


# ---------------------------------------------------------------- 事件分类


@pytest.mark.parametrize(
    "title, expected",
    [
        ("某公司收到证监会立案告知书", "监管处罚"),
        ("某公司股票临时停牌公告", "停复牌"),
        ("某公司拟收购某标的 100% 股权", "并购重组"),
        ("某公司发布业绩预告:预增 120%", "业绩"),
        ("某公司中标 5 亿元项目", "订单合同"),
        ("控股股东计划减持不超过 2%", "股东行为"),
        ("某公司拟发行可转债募集 10 亿元", "融资"),
        ("某公司 2025 年度利润分配方案", "分红"),
        ("某公司总经理辞职", "人事"),
        ("发改委发布新一轮补贴政策", "政策"),
        ("某公司今日涨停,登上龙虎榜", "交易异动"),
    ],
)
def test_classify_event_covers_declared_categories(title, expected):
    assert classify_event(title) == expected


def test_classify_event_returns_none_when_nothing_matches():
    """不设"其他"兜底类:统计出来的事件分布不能是规则覆盖率的镜像。"""
    assert classify_event("某公司参加行业展会并作主题分享") is None


def test_classify_event_priority_prefers_penalty_over_earnings():
    """同时提到处罚与业绩时,判成监管处罚更贴近实际影响。"""
    got = classify_event("某公司业绩预增,同时因信息披露违规收到警示函")
    assert got == "监管处罚"


def test_classify_event_reads_summary_too():
    assert classify_event("某公司公告", "公司拟回购股份") == "股东行为"


# ---------------------------------------------------------------- 情绪


def test_judge_sentiment_positive():
    got = judge_sentiment("某公司业绩预增,产品提价")
    assert got.sentiment == "positive"
    assert got.score is not None and got.score > 0
    assert "预增" in got.positive_hits


def test_judge_sentiment_negative():
    got = judge_sentiment("某公司亏损扩大,遭监管处罚")
    assert got.sentiment == "negative"
    assert got.score is not None and got.score < 0
    assert got.negative_hits


def test_judge_sentiment_balanced_evidence_is_neutral_not_none():
    """有正有负、彼此抵消 -> 有证据的中性,score 落在中性带内。"""
    got = judge_sentiment("某公司主业预增,但子公司亏损")
    assert got.sentiment == "neutral"
    assert got.score == pytest.approx(0.0)
    assert got.evidence


def test_judge_sentiment_without_evidence_is_none_not_zero():
    """判不出就是判不出。给 0 会在页面上冒充"评估过、结论中性"。"""
    got = judge_sentiment("某公司发布日常经营公告")
    assert got.sentiment is None
    assert got.score is None
    assert got.evidence == ()


def test_judge_sentiment_as_dict_keys():
    keys = set(judge_sentiment("某公司涨停").as_dict())
    assert keys == {"sentiment", "sentiment_score", "positive_hits", "negative_hits"}


# ---------------------------------------------------------------- 可信度


def test_score_credibility_penalises_missing_summary():
    full = score_credibility(base=0.9, has_summary=True)
    thin = score_credibility(base=0.9, has_summary=False)
    assert full == pytest.approx(0.9)
    assert thin is not None and thin < full


def test_score_credibility_returns_none_without_base():
    """来源没登记基准可信度就返回 None,不能凭空给 0.5 冒充"中等可信"。"""
    assert score_credibility(base=None, has_summary=True) is None


def test_score_credibility_clamps_to_unit_range():
    assert score_credibility(base=0.0, has_summary=False) == 0.0


@pytest.mark.parametrize("base", [-0.1, 1.5])
def test_score_credibility_rejects_out_of_range_base(base):
    with pytest.raises(NewsTextError):
        score_credibility(base=base, has_summary=True)


# ---------------------------------------------------------------- 关联

UNIVERSE = (
    StockRef(ts_code="600519.SH", symbol="600519", name="贵州茅台", industry="白酒"),
    StockRef(ts_code="000001.SZ", symbol="000001", name="平安银行", industry="银行"),
    StockRef(ts_code="601012.SH", symbol="601012", name="隆基绿能", industry="光伏设备"),
    # 两字名:只靠名字匹配误命中率太高,规则要求名字长度达到下限才允许关联
    StockRef(ts_code="600000.SH", symbol="600000", name="浦发", industry="银行"),
)
INDUSTRY_OF = {ref.ts_code: ref.industry for ref in UNIVERSE}


def test_link_stocks_by_declared_code_has_top_confidence():
    """来源结构化字段给出的代码最可信,且不被正文匹配覆盖。"""
    links = link_stocks(
        title="关于经营情况的说明",
        summary=None,
        universe=UNIVERSE,
        declared_codes=("600519.SH",),
    )
    assert [link.link_key for link in links] == ["600519.SH"]
    assert links[0].match_basis == "source_field"
    assert links[0].confidence == 1.0


def test_link_stocks_by_code_in_text():
    links = link_stocks(
        title="600519 今日成交额居前", summary=None, universe=UNIVERSE
    )
    assert [link.link_key for link in links] == ["600519.SH"]
    assert links[0].match_basis == "code_in_text"
    assert links[0].match_text == "600519"


def test_link_stocks_by_name_in_text():
    links = link_stocks(title="隆基绿能公布新产能规划", summary=None, universe=UNIVERSE)
    assert [link.link_key for link in links] == ["601012.SH"]
    assert links[0].match_basis == "name_in_text"
    assert links[0].confidence == pytest.approx(0.75)


def test_link_stocks_skips_short_names():
    """"浦发"两字,单靠名字出现不足以支撑关联。"""
    assert MIN_NAME_MATCH_LEN >= 3
    links = link_stocks(title="浦发地块完成出让", summary=None, universe=UNIVERSE)
    assert links == ()


def test_link_stocks_returns_empty_without_any_basis():
    """没有依据就是零条关联,而不是"关联到全市场"。"""
    assert link_stocks(title="市场综述", summary=None, universe=UNIVERSE) == ()


def test_link_stocks_declared_code_beats_text_name():
    links = link_stocks(
        title="贵州茅台相关报道",
        summary=None,
        universe=UNIVERSE,
        declared_codes=("600519",),
    )
    assert links[0].match_basis == "source_field"


def test_link_stocks_dedups_and_sorts():
    links = link_stocks(
        title="平安银行(000001)与隆基绿能(601012)同时发布公告",
        summary="平安银行相关",
        universe=UNIVERSE,
    )
    assert [link.link_key for link in links] == ["000001.SZ", "601012.SH"]


def test_link_stocks_every_link_carries_basis():
    """match_basis 必填是入库前提;没有依据的行不允许存在。"""
    links = link_stocks(title="隆基绿能 600519 公告", summary=None, universe=UNIVERSE)
    assert links
    for link in links:
        assert link.match_basis
        assert link.match_text
        assert 0 < link.confidence <= 1.0


def test_link_industries_separates_two_bases():
    """正文点名的行业与"个股所属行业"强弱不同,必须分开记录。"""
    stock_links = link_stocks(
        title="光伏设备板块走强,隆基绿能领涨", summary=None, universe=UNIVERSE
    )
    industries = link_industries(
        title="光伏设备板块走强,隆基绿能领涨",
        summary=None,
        stock_links=stock_links,
        industry_of=INDUSTRY_OF,
        known_industries=("光伏设备", "银行"),
    )
    assert [link.link_key for link in industries] == ["光伏设备"]
    # 正文点名优先,不被 via_linked_stock 降级覆盖
    assert industries[0].match_basis == "industry_name_in_text"


def test_link_industries_via_linked_stock_is_weaker():
    stock_links = link_stocks(
        title="隆基绿能公布新产能规划", summary=None, universe=UNIVERSE
    )
    industries = link_industries(
        title="隆基绿能公布新产能规划",
        summary=None,
        stock_links=stock_links,
        industry_of=INDUSTRY_OF,
        known_industries=("银行",),
    )
    assert [link.link_key for link in industries] == ["光伏设备"]
    link = industries[0]
    assert link.match_basis == "via_linked_stock"
    assert link.match_text == "601012.SH"
    assert link.confidence == pytest.approx(0.6 * 0.75)


def test_link_industries_skips_unknown_industry():
    """个股行业字段缺失时不编造行业关联。"""
    stock_links = (
        link_stocks(title="隆基绿能公告", summary=None, universe=UNIVERSE)
    )
    industries = link_industries(
        title="隆基绿能公告",
        summary=None,
        stock_links=stock_links,
        industry_of={"601012.SH": None},
    )
    assert industries == ()
