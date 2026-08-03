"""舆情采集编排层集成测试:离线闭环、幂等重跑、来源追溯、失败暴露。

用进程内假采集器 + tmp_path 下的临时 DuckDB。绝不触碰真实库
(workbench/data/market.duckdb)——每个用例的 store 都建在 tmp_path 下。

假采集器只替代"网络获取"这一件事:它返回的 RawNewsItem 与真实采集器返回的
是同一种结构,后面的归一化、去重、关联、入库走的都是生产代码路径。这不是
Mock 业务逻辑,而是把不可离线复现的 IO 边界参数化。

锁定的行为:
- 重跑同一天不产生重复行(news_id 幂等)。
- 转载只标不删,原文与转载都完整入库。
- 采集器抛错 -> 整批中止上抛,不静默降级。
- 单条不合格 -> 记进 rejected 并给出原因,不悄悄丢弃。
- 没有启用来源 -> fetched=0 且 sources 为空,可与"采到 0 条"区分。
- 每条入库记录都能回溯到 source_id / url / published_at。

运行:
    cd workbench
    python -m pytest tests/test_news.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.db import Store  # noqa: E402
from engine.news import (  # noqa: E402
    NewsCollectError,
    NewsSource,
    RawNewsItem,
    collect_news,
)

pytestmark = pytest.mark.integration

TRADE_DATE = "20260731"
NEXT_TRADE_DATE = "20260803"
_CAL_DATES = [
    ("20260727", 1),
    ("20260728", 1),
    ("20260729", 1),
    ("20260730", 1),
    ("20260731", 1),
    ("20260801", 0),
    ("20260802", 0),
    ("20260803", 1),
]

_STOCKS = [
    ("601012.SH", "601012", "隆基绿能", "光伏设备"),
    ("600519.SH", "600519", "贵州茅台", "白酒"),
    ("000001.SZ", "000001", "平安银行", "银行"),
]


# ---------------------------------------------------------------- 夹具


def _seed(store: Store) -> None:
    """写入交易日历、股票基础信息与目标日行情截面。"""
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
    rows = []
    for code, _symbol, _name, _industry in _STOCKS:
        for cal_date, is_open in _CAL_DATES:
            if not is_open:
                continue
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": cal_date,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "pre_close": 10.0,
                    "pct_chg": 2.0,
                    "vol": 1e6,
                    "amount": 1e7,
                }
            )
    store.upsert("daily", pd.DataFrame(rows), keys=("ts_code", "trade_date"))


@pytest.fixture()
def store(tmp_path: Path):
    """隔离数据库。用 tmp_path,绝不指向 workbench/data 下的真实库。"""
    path = tmp_path / "news_test.duckdb"
    with Store(path) as st:
        _seed(st)
        yield st


class FakeFetcher:
    """进程内采集器:只替代网络获取,不改变任何业务判定。"""

    def __init__(self, source: NewsSource, items, *, error: Exception | None = None):
        self._source = source
        self._items = list(items)
        self._error = error
        self.calls: list[tuple[str, datetime, datetime]] = []

    @property
    def source(self) -> NewsSource:
        return self._source

    def fetch(self, *, trade_date, window_start, window_end):
        self.calls.append((trade_date, window_start, window_end))
        if self._error is not None:
            raise self._error
        return list(self._items)


def _source(source_id="demo", *, enabled=True, credibility=0.9, kind="news") -> NewsSource:
    return NewsSource(
        source_id=source_id,
        name=f"测试来源-{source_id}",
        kind=kind,
        home_url="https://example.com",
        base_credibility=credibility,
        compliance_note="测试用进程内来源,不发起任何网络请求",
        enabled=enabled,
    )


def _item(**kw) -> RawNewsItem:
    base = dict(
        source_id="demo",
        title="隆基绿能发布业绩预告:预增 120%",
        url="https://example.com/news/1",
        published_at="2026-07-31 10:00:00",
        summary="公司预计上半年净利润同比预增。",
        declared_codes=(),
        raw={"origin": "fake"},
    )
    base.update(kw)
    return RawNewsItem(**base)


def _items_table(store: Store) -> pd.DataFrame:
    return store.con.execute(
        "SELECT * FROM news_items ORDER BY published_at, news_id"
    ).df()


# ---------------------------------------------------------------- 来源登记


def test_source_requires_compliance_note():
    """没有"凭什么能采"这句话的来源不许登记,事后无从审计。"""
    with pytest.raises(NewsCollectError) as excinfo:
        NewsSource(
            source_id="x",
            name="某来源",
            kind="news",
            home_url="https://e.com",
            base_credibility=0.5,
            compliance_note="   ",
            enabled=True,
        )
    assert "合规备注" in str(excinfo.value)


def test_source_rejects_unknown_kind():
    with pytest.raises(NewsCollectError):
        NewsSource(
            source_id="x",
            name="某来源",
            kind="weibo",
            home_url="https://e.com",
            base_credibility=0.5,
            compliance_note="官方接口",
            enabled=True,
        )


def test_source_requires_id_and_name():
    with pytest.raises(NewsCollectError):
        NewsSource(
            source_id="",
            name="某来源",
            kind="news",
            home_url="https://e.com",
            base_credibility=None,
            compliance_note="官方接口",
            enabled=True,
        )


def test_disabled_sources_are_still_registered(store: Store):
    """未启用的来源也要落库:页面要能解释"为什么这个来源没有数据"。"""
    fetcher = FakeFetcher(_source("off", enabled=False), [_item(source_id="off")])
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.fetched == 0
    assert result.sources == []
    assert fetcher.calls == []  # 未启用来源根本不该被调用

    rows = store.con.execute("SELECT source_id, enabled FROM news_sources").df()
    assert rows["source_id"].tolist() == ["off"]
    assert not bool(rows["enabled"].iloc[0])


# ---------------------------------------------------------------- 基本入库与追溯


def test_collect_stores_traceable_rows(store: Store):
    """每条记录都必须能回到来源、原始链接与发布时间。"""
    fetcher = FakeFetcher(_source(), [_item()])
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.fetched == 1
    assert result.stored == 1
    assert result.rejected == []
    assert result.sources == ["demo"]

    rows = _items_table(store)
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["source_id"] == "demo"
    assert row["url"] == "https://example.com/news/1"
    assert row["published_at"] == "2026-07-31T10:00:00"
    assert row["fetched_at"]
    assert row["trade_date"] == TRADE_DATE
    assert row["event_type"] == "业绩"
    assert row["sentiment"] == "positive"
    assert row["credibility"] == pytest.approx(0.9)


def test_collect_window_is_previous_close_to_target_close(store: Store):
    """采集窗口正是"会归属到目标日"的那段区间。"""
    fetcher = FakeFetcher(_source(), [])
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    trade_date, start, end = fetcher.calls[0]
    assert trade_date == TRADE_DATE
    assert start == datetime(2026, 7, 30, 15, 0)
    assert end == datetime(2026, 7, 31, 15, 0)


def test_raw_json_records_derived_judgements(store: Store):
    """派生判断连同证据一起留痕,页面上"为什么判成利好"要能答得出来。"""
    fetcher = FakeFetcher(_source(), [_item()])
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    payload = json.loads(_items_table(store).iloc[0]["raw_json"])
    assert payload["source"] == {"origin": "fake"}
    derived = payload["derived"]
    assert derived["sentiment"]["sentiment"] == "positive"
    assert derived["sentiment"]["positive_hits"]
    assert 0 < derived["decay_at_collect"] <= 1.0


def test_missing_summary_stays_null_and_lowers_credibility(store: Store):
    """来源没给摘要就留 NULL,不用标题顶替。"""
    fetcher = FakeFetcher(_source(), [_item(summary=None)])
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    row = _items_table(store).iloc[0]
    assert row["summary"] is None or pd.isna(row["summary"])
    assert row["credibility"] == pytest.approx(0.8)


def test_credibility_is_null_when_source_has_no_base(store: Store):
    """来源没登记基准可信度 -> NULL,而不是一个看起来评估过的默认值。"""
    fetcher = FakeFetcher(_source(credibility=None), [_item()])
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    value = _items_table(store).iloc[0]["credibility"]
    assert value is None or pd.isna(value)


def test_unclassifiable_item_keeps_nulls(store: Store):
    """判不出事件与情绪就留 NULL,不塞"其他"、不塞 0。"""
    fetcher = FakeFetcher(
        _source(),
        [_item(title="隆基绿能参加行业展会", summary="公司参展并作主题分享。")],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    row = _items_table(store).iloc[0]
    assert row["event_type"] is None or pd.isna(row["event_type"])
    assert row["sentiment"] is None or pd.isna(row["sentiment"])
    assert row["sentiment_score"] is None or pd.isna(row["sentiment_score"])


# ---------------------------------------------------------------- 时间边界


def test_post_close_news_rolls_to_next_trade_date(store: Store):
    """收盘后发布的消息不能用来解释当天走势。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(url="https://example.com/a", published_at="2026-07-31 14:30:00"),
            _item(url="https://example.com/b", published_at="2026-07-31 19:30:00"),
        ],
    )
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.by_trade_date == {TRADE_DATE: 1, NEXT_TRADE_DATE: 1}
    rows = _items_table(store).set_index("url")
    assert rows.loc["https://example.com/a", "trade_date"] == TRADE_DATE
    assert rows.loc["https://example.com/b", "trade_date"] == NEXT_TRADE_DATE


def test_news_by_trade_date_excludes_next_day_items(store: Store):
    """按交易日取数时,归属到下一日的条目不会污染当日复盘。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(url="https://example.com/a", published_at="2026-07-31 09:30:00"),
            _item(
                url="https://example.com/b",
                title="贵州茅台发布年报",
                published_at="2026-07-31 20:00:00",
            ),
        ],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    today = store.news_by_trade_date(TRADE_DATE)
    assert today["url"].tolist() == ["https://example.com/a"]


def test_snapshot_item_without_calendar_coverage_is_stored(store: Store):
    """热榜快照(采集时刻晚于日历末尾)不被拒收:归属日历最后一个交易日。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(
                url="https://example.com/snap1",
                title="贵州茅台创阶段新高",
                published_at="2026-08-01 17:37:18",
                raw={
                    "time_basis": "first_seen_at_collect",
                    "provider": "trendradar",
                },
            ),
        ],
    )
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.rejected == []
    assert result.stored == 1
    rows = _items_table(store)
    assert len(rows) == 1
    assert rows.iloc[0]["trade_date"] == TRADE_DATE
    assert rows.iloc[0]["raw_json"] != ""

# ---------------------------------------------------------------- 去重与幂等


def test_rerun_is_idempotent(store: Store):
    """同一天重跑不产生重复行——批次重跑是常态,不能每跑一次就多一份。"""
    fetcher = FakeFetcher(_source(), [_item()])
    first = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])
    second = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert first.stored == 1
    assert second.stored == 1
    assert len(_items_table(store)) == 1
    assert (
        store.con.execute("SELECT COUNT(*) FROM news_links").fetchone()[0]
        == store.con.execute(
            "SELECT COUNT(DISTINCT (news_id, link_type, link_key)) FROM news_links"
        ).fetchone()[0]
    )


def test_tracking_params_do_not_create_duplicate_rows(store: Store):
    """同一篇文章带不同跟踪参数被采到两次,仍然只有一行。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(url="https://example.com/news/1?utm_source=wx"),
            _item(url="https://example.com/news/1?utm_source=app#top"),
        ],
    )
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.fetched == 2
    assert len(_items_table(store)) == 1


def test_reprints_are_marked_not_deleted(store: Store):
    """转载标 duplicate_of,但原文与转载都完整保留——谁在转本身就是热度证据。"""
    first = FakeFetcher(
        _source("first", credibility=0.9),
        [
            _item(
                source_id="first",
                url="https://first.com/1",
                published_at="2026-07-31 09:00:00",
            )
        ],
    )
    second = FakeFetcher(
        _source("second", credibility=0.6),
        [
            _item(
                source_id="second",
                title="【转载】隆基绿能发布业绩预告:预增 120%!",
                url="https://second.com/9",
                published_at="2026-07-31 11:00:00",
            )
        ],
    )
    result = collect_news(
        store=store, trade_date=TRADE_DATE, fetchers=[first, second]
    )

    assert result.fetched == 2
    assert result.stored == 2
    assert result.duplicates == 1

    rows = _items_table(store).set_index("url")
    assert len(rows) == 2
    original_id = rows.loc["https://first.com/1", "news_id"]
    assert pd.isna(rows.loc["https://first.com/1", "duplicate_of"]) or (
        rows.loc["https://first.com/1", "duplicate_of"] is None
    )
    assert rows.loc["https://second.com/9", "duplicate_of"] == original_id

    # 默认视图只给一条证据,需要时能看到全量
    assert len(store.news_by_trade_date(TRADE_DATE)) == 1
    assert len(store.news_by_trade_date(TRADE_DATE, include_duplicates=True)) == 2


def test_reprints_do_not_inflate_links(store: Store):
    """转载不重复建关联,否则行业热度会被转载量放大。"""
    first = FakeFetcher(
        _source("first"),
        [_item(source_id="first", url="https://first.com/1", published_at="2026-07-31 09:00:00")],
    )
    second = FakeFetcher(
        _source("second"),
        [
            _item(
                source_id="second",
                title="隆基绿能发布业绩预告:预增 120%",
                url="https://second.com/9",
                published_at="2026-07-31 11:00:00",
            )
        ],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[first, second])

    links = store.con.execute(
        "SELECT news_id, link_type, link_key FROM news_links"
    ).df()
    assert links["news_id"].nunique() == 1


def test_same_headline_on_different_days_is_not_merged(store: Store):
    """去重指纹按天分组:两个月后的同名标题是新消息,不是转载。"""
    fetcher_a = FakeFetcher(
        _source(),
        [_item(url="https://example.com/a", published_at="2026-07-30 10:00:00")],
    )
    fetcher_b = FakeFetcher(
        _source(),
        [_item(url="https://example.com/b", published_at="2026-07-31 10:00:00")],
    )
    collect_news(store=store, trade_date="20260730", fetchers=[fetcher_a])
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher_b])

    assert result.duplicates == 0
    rows = _items_table(store)
    assert len(rows) == 2
    assert rows["duplicate_of"].isna().all()


# ---------------------------------------------------------------- 关联


def test_links_carry_match_basis(store: Store):
    """没有匹配依据的关联一行都不许入库。"""
    fetcher = FakeFetcher(_source(), [_item()])
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    links = store.con.execute("SELECT * FROM news_links").df()
    assert not links.empty
    assert (links["match_basis"].str.len() > 0).all()
    assert (links["confidence"] > 0).all()

    stock = links[links["link_type"] == "stock"]
    assert stock["link_key"].tolist() == ["601012.SH"]
    assert stock["match_basis"].iloc[0] == "name_in_text"

    industry = links[links["link_type"] == "industry"]
    assert industry["link_key"].tolist() == ["光伏设备"]
    assert industry["match_basis"].iloc[0] == "via_linked_stock"


def test_declared_codes_win_over_text(store: Store):
    """来源结构化字段给出的代码可信度最高。"""
    fetcher = FakeFetcher(
        _source(kind="notice"),
        [
            _item(
                title="关于回购股份的进展公告",
                summary="公司披露回购进展。",
                declared_codes=("600519.SH",),
            )
        ],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    links = store.con.execute(
        "SELECT link_key, match_basis, confidence FROM news_links WHERE link_type = 'stock'"
    ).df()
    assert links["link_key"].tolist() == ["600519.SH"]
    assert links["match_basis"].iloc[0] == "source_field"
    assert links["confidence"].iloc[0] == pytest.approx(1.0)


def test_unrelated_news_produces_no_links(store: Store):
    """关联不上就是零条,不做"猜一个最像的"。"""
    fetcher = FakeFetcher(
        _source(),
        [_item(title="市场今日成交额小幅回落", summary="两市成交额环比下降。")],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert store.con.execute("SELECT COUNT(*) FROM news_links").fetchone()[0] == 0
    assert len(_items_table(store)) == 1  # 条目本身仍要入库


def test_news_for_link_respects_as_of(store: Store):
    """按 as_of 过滤是前视纪律的一部分。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(url="https://example.com/a", published_at="2026-07-30 10:00:00"),
            _item(url="https://example.com/b", published_at="2026-07-31 10:00:00"),
        ],
    )
    collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    early = store.news_for_link(
        link_type="stock", link_key="601012.SH", as_of="20260730"
    )
    later = store.news_for_link(link_type="stock", link_key="601012.SH")
    assert len(early) < len(later)


# ---------------------------------------------------------------- 失败暴露


def test_fetcher_error_aborts_batch(store: Store):
    """采集器抛错就整批中止上抛,不静默降级成"今天没有舆情"。"""
    bad = FakeFetcher(_source("bad"), [], error=TimeoutError("连接超时"))
    with pytest.raises(NewsCollectError) as excinfo:
        collect_news(store=store, trade_date=TRADE_DATE, fetchers=[bad])

    message = str(excinfo.value)
    assert "bad" in message
    assert "TimeoutError" in message
    assert store.con.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 0


def test_bad_item_is_rejected_with_reason(store: Store):
    """单条不合格不拖垮整批,但拒收原因必须逐条留下。"""
    fetcher = FakeFetcher(
        _source(),
        [
            _item(url="https://example.com/ok"),
            _item(url="https://example.com/bad", published_at="上周三"),
            _item(url="https://example.com/empty", title="   "),
        ],
    )
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[fetcher])

    assert result.fetched == 3
    assert result.stored == 1
    assert len(result.rejected) == 2
    reasons = {r["url"]: r["reason"] for r in result.rejected}
    assert "上周三" in reasons["https://example.com/bad"]
    assert reasons["https://example.com/empty"]
    assert result.as_dict()["rejected_count"] == 2


def test_missing_calendar_is_a_hard_failure(tmp_path: Path):
    """交易日历没更新是需要人处理的真实故障,不能猜一个日期继续。"""
    with Store(tmp_path / "empty.duckdb") as st:
        fetcher = FakeFetcher(_source(), [_item()])
        with pytest.raises(NewsCollectError) as excinfo:
            collect_news(store=st, trade_date=TRADE_DATE, fetchers=[fetcher])
        assert "trade_cal" in str(excinfo.value)


def test_missing_snapshot_is_a_hard_failure(tmp_path: Path):
    """没有行情截面就无法建立关联,直接失败而不是产出无关联的舆情。"""
    with Store(tmp_path / "nodaily.duckdb") as st:
        st.upsert(
            "trade_cal",
            pd.DataFrame(
                [{"exchange": "SSE", "cal_date": d, "is_open": o} for d, o in _CAL_DATES]
            ),
            keys=("exchange", "cal_date"),
        )
        fetcher = FakeFetcher(_source(), [_item()])
        with pytest.raises(NewsCollectError) as excinfo:
            collect_news(store=st, trade_date=TRADE_DATE, fetchers=[fetcher])
        assert "行情" in str(excinfo.value)


def test_no_fetchers_at_all_is_empty_not_error(store: Store):
    """一个来源都没配 -> 空结果,由调用方标成"未配置",不是失败也不是"0 条新闻"。"""
    result = collect_news(store=store, trade_date=TRADE_DATE, fetchers=[])
    assert result.fetched == 0
    assert result.stored == 0
    assert result.sources == []
    assert result.as_dict()["trade_date"] == TRADE_DATE
