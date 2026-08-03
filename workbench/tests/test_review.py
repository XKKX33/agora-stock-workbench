"""复盘装配单测:标注体系、缺数据暴露、前视纪律。

全部用 tmp_path 下的临时 DuckDB,绝不触碰真实库
(workbench/data/market.duckdb)。不发起任何网络请求。

锁定的行为:
- 每一节都带 label,且 label 只能是 fact / derived / unverified 三者之一。
- 缺数据的节 available=False + missing_reason,**不返回 0 或空列表冒充已算**。
- "没登记来源""登记了没采过""采了当天没有"三种缺失可区分。
- 舆情与价格的对应关系按 as_of 截断,不含复盘日之后发布的内容(前视纪律)。
- 关联条目必须带 match_basis,说明"凭什么认为这条新闻和这只股票有关"。
- 情绪与事件分类判不出时为 None,不填 0 冒充中性。

运行:
    cd workbench
    python -m pytest tests/test_review.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.db import Store  # noqa: E402
from engine.review import (  # noqa: E402
    LABEL_DERIVED,
    LABEL_FACT,
    LABEL_LEGEND,
    LABEL_UNVERIFIED,
    build_review,
)

pytestmark = pytest.mark.integration

TRADE_DATE = "20260731"
PREV_DATE = "20260730"
NEXT_DATE = "20260803"
STRATEGY = "test_review"
RUN_ID = "run-review-001"

_CAL = [
    ("20260729", 1),
    ("20260730", 1),
    ("20260731", 1),
    ("20260801", 0),
    ("20260802", 0),
    ("20260803", 1),
]

# ts_code, name, industry, pct_chg, rank, total, passed, selected, money_class
_ROWS = [
    ("601012.SH", "隆基绿能", "光伏设备", 9.8, 1, 0.91, True, True, "主力流入"),
    ("600519.SH", "贵州茅台", "白酒", 1.2, 2, 0.77, True, True, None),
    ("000001.SZ", "平安银行", "银行", -0.5, 3, 0.61, True, False, "主力流出"),
    ("600036.SH", "招商银行", "银行", -1.4, 4, 0.40, False, False, None),
]


# ---------------------------------------------------------------- 夹具


def _seed_market(store: Store) -> None:
    store.upsert(
        "trade_cal",
        pd.DataFrame([{"exchange": "SSE", "cal_date": d, "is_open": o} for d, o in _CAL]),
        keys=("exchange", "cal_date"),
    )
    store.upsert(
        "stock_basic",
        pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "symbol": code.split(".")[0],
                    "name": name,
                    "area": "",
                    "industry": industry,
                    "market": "主板",
                    "list_date": "20100101",
                }
                for code, name, industry, *_ in _ROWS
            ]
        ),
        keys=("ts_code",),
    )
    daily = []
    for code, _name, _ind, pct, *_ in _ROWS:
        for cal_date, is_open in _CAL:
            if not is_open:
                continue
            daily.append(
                {
                    "ts_code": code,
                    "trade_date": cal_date,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": 10.0 * (1 + pct / 100),
                    "pre_close": 10.0,
                    "pct_chg": pct,
                    "vol": 1e6,
                    "amount": 1e8,
                }
            )
    store.upsert("daily", pd.DataFrame(daily), keys=("ts_code", "trade_date"))


def _seed_scan(store: Store, *, as_of: str = TRADE_DATE) -> None:
    rows = []
    for code, name, industry, _pct, rank, total, passed, selected, money in _ROWS:
        rows.append(
            {
                "run_id": RUN_ID,
                "ts_code": code,
                "name": name,
                "industry": industry,
                "rank": rank,
                "total": total,
                "passed": passed,
                "selected": selected,
                "gate_reasons_json": json.dumps(
                    [] if passed else ["量能不足", "跌破均线"], ensure_ascii=False
                ),
                "cat_scores_json": json.dumps(
                    {"structure": 0.8, "theme": 0.6, "money": 0.4}, ensure_ascii=False
                ),
                "money_class": money,
                "one_line": f"{name} 归因",
                "contrib_json": json.dumps(
                    {"ma_slope": 0.30, "industry_lead": 0.20, "vol_ratio": 0.10},
                    ensure_ascii=False,
                ),
                "feat_json": json.dumps({"close": 10.0}, ensure_ascii=False),
            }
        )
    run_row = {
        "run_id": RUN_ID,
        "run_date": as_of,
        "as_of": as_of,
        "strategy": STRATEGY,
        "candidate_count": 120,
        "scored_count": len(_ROWS),
        "passed_count": sum(1 for r in _ROWS if r[6]),
        "final_count": sum(1 for r in _ROWS if r[7]),
        "top_industries_json": json.dumps(
            [
                {
                    "industry": "光伏设备",
                    "count": 30,
                    "avg_pct": 3.1,
                    "med_pct": 2.8,
                    "up_ratio": 0.8,
                    "strong_ratio": 0.2,
                    "total_amount": 5e9,
                    "heat": 12.5,
                },
                {
                    "industry": "银行",
                    "count": 40,
                    "avg_pct": -0.9,
                    "med_pct": -1.0,
                    "up_ratio": 0.2,
                    "strong_ratio": 0.0,
                    "total_amount": 3e9,
                    "heat": 4.2,
                },
            ],
            ensure_ascii=False,
        ),
    }
    store.record_scan(run_row, pd.DataFrame(rows))


def _news_row(
    news_id: str,
    *,
    trade_date: str,
    title: str,
    sentiment=None,
    sentiment_score=None,
    event_type=None,
    credibility=0.8,
    duplicate_of=None,
) -> dict:
    return {
        "news_id": news_id,
        "source_id": "demo",
        "title": title,
        "summary": "摘要正文",
        "url": f"https://example.com/{news_id}",
        "published_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 10:00:00",
        "fetched_at": "2026-07-31T16:00:00",
        "trade_date": trade_date,
        "dedup_key": f"dk-{news_id}",
        "duplicate_of": duplicate_of,
        "event_type": event_type,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "credibility": credibility,
        "raw_json": "{}",
    }


def _seed_news(store: Store) -> None:
    """登记来源并写入三条新闻:当日两条 + 复盘日之后一条(前视诱饵)。"""
    store.upsert_news_sources(
        pd.DataFrame(
            [
                {
                    "source_id": "demo",
                    "name": "测试来源",
                    "kind": "news",
                    "home_url": "https://example.com",
                    "base_credibility": 0.9,
                    "compliance_note": "测试用进程内来源,不发起网络请求",
                    "enabled": True,
                }
            ]
        )
    )
    store.upsert_news_items(
        pd.DataFrame(
            [
                _news_row(
                    "n1",
                    trade_date=TRADE_DATE,
                    title="隆基绿能发布业绩预告",
                    sentiment="positive",
                    sentiment_score=0.7,
                    event_type="业绩预告",
                ),
                # 判不出情绪的一条:sentiment / score 都是 None,不能被填成 0
                _news_row("n2", trade_date=TRADE_DATE, title="某公司公告人事变动"),
                # 复盘日之后发布:对应关系一节绝不能读到它
                _news_row(
                    "n3",
                    trade_date=NEXT_DATE,
                    title="隆基绿能后续追踪报道",
                    sentiment="negative",
                    sentiment_score=-0.5,
                ),
            ]
        )
    )
    store.upsert_news_links(
        pd.DataFrame(
            [
                {
                    "news_id": "n1",
                    "link_type": "stock",
                    "link_key": "601012.SH",
                    "match_basis": "name_in_text",
                    "match_text": "隆基绿能",
                    "confidence": 0.9,
                },
                {
                    "news_id": "n3",
                    "link_type": "stock",
                    "link_key": "601012.SH",
                    "match_basis": "name_in_text",
                    "match_text": "隆基绿能",
                    "confidence": 0.9,
                },
                {
                    "news_id": "n1",
                    "link_type": "industry",
                    "link_key": "光伏设备",
                    "match_basis": "source_field",
                    "match_text": "光伏设备",
                    "confidence": 0.6,
                },
            ]
        )
    )


@pytest.fixture()
def store(tmp_path: Path):
    """隔离数据库。用 tmp_path,绝不指向 workbench/data 下的真实库。"""
    with Store(tmp_path / "review_test.duckdb") as st:
        _seed_market(st)
        yield st


@pytest.fixture()
def full_store(store: Store):
    _seed_scan(store)
    _seed_news(store)
    return store


def _section(review: dict, key: str) -> dict:
    return review["sections"][key]


# ---------------------------------------------------------------- 结构与标注


def test_all_required_sections_present(full_store: Store):
    """九项复盘内容一个都不能少,少一节就是没交付。"""
    review = build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    assert set(review["sections"]) == {
        "market_structure",
        "industry_heat",
        "selection",
        "factor_contribution",
        "money_confirmation",
        "news_highlights",
        "news_alignment",
        "prediction_review",
    }


def test_every_section_is_labelled(full_store: Store):
    """标注必须落在三类之内:事实 / 规则计算结果 / 待验证判断。"""
    review = build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    valid = {LABEL_FACT, LABEL_DERIVED, LABEL_UNVERIFIED}
    for key, section in review["sections"].items():
        assert section["label"] in valid, key
        assert section["title"], key
    assert set(LABEL_LEGEND) == valid


def test_labels_match_semantics(full_store: Store):
    """市场结构是事实,行业热度是公式产物,舆情对应是待验证判断。"""
    review = build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    assert _section(review, "market_structure")["label"] == LABEL_FACT
    assert _section(review, "news_highlights")["label"] == LABEL_FACT
    assert _section(review, "industry_heat")["label"] == LABEL_DERIVED
    assert _section(review, "selection")["label"] == LABEL_DERIVED
    assert _section(review, "factor_contribution")["label"] == LABEL_DERIVED
    assert _section(review, "money_confirmation")["label"] == LABEL_DERIVED
    assert _section(review, "prediction_review")["label"] == LABEL_DERIVED
    assert _section(review, "news_alignment")["label"] == LABEL_UNVERIFIED


def test_result_is_json_serializable(full_store: Store):
    """结果要能原样进 API 响应,不能带 numpy / Timestamp。"""
    review = build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    json.dumps(review, ensure_ascii=False)


# ---------------------------------------------------------------- 各节内容


def test_market_structure_counts_are_facts(full_store: Store):
    data = _section(full_store_review(full_store), "market_structure")["data"]
    assert data["total_symbols"] == len(_ROWS)
    assert data["quoted_symbols"] == len(_ROWS)
    assert data["missing_pct_chg"] == 0
    assert data["up"] == 2  # 9.8 与 1.2
    assert data["down"] == 2  # -0.5 与 -1.4
    assert data["flat"] == 0
    assert data["near_limit_up"] == 1  # 只有 9.8 >= 9.5


def full_store_review(store: Store) -> dict:
    return build_review(store, trade_date=TRADE_DATE, strategy=STRATEGY)


def test_industry_heat_ordered(full_store: Store):
    data = _section(full_store_review(full_store), "industry_heat")["data"]
    assert [row["industry"] for row in data] == ["光伏设备", "银行"]
    assert data[0]["heat"] == 12.5
    assert data[0]["count"] == 30 and isinstance(data[0]["count"], int)


def test_selection_separates_rejected_from_truncated(full_store: Store):
    """被规则否掉和被排名截断是两回事,不能合并成一个"没入选"。"""
    data = _section(full_store_review(full_store), "selection")["data"]
    assert data["scored"] == 4
    assert data["passed"] == 3
    assert data["selected"] == 2
    assert data["passed_not_selected"] == 1  # 平安银行:过了门槛但没进最终名单
    assert data["rejected"] == 1  # 招商银行:被门槛否掉
    assert {row["reason"] for row in data["reject_reasons"]} == {"量能不足", "跌破均线"}
    assert [row["ts_code"] for row in data["selected_list"]] == ["601012.SH", "600519.SH"]


def test_factor_contribution_only_over_selected(full_store: Store):
    data = _section(full_store_review(full_store), "factor_contribution")["data"]
    assert data["n_selected"] == 2
    top = data["by_factor"][0]
    assert top["key"] == "ma_slope"
    assert top["avg"] == pytest.approx(0.30)
    assert top["n"] == 2
    assert {row["key"] for row in data["by_category"]} == {"structure", "theme", "money"}


def test_money_unclassified_is_not_a_class(full_store: Store):
    """缺资金流数据不等于"资金未确认",必须单列。"""
    data = _section(full_store_review(full_store), "money_confirmation")["data"]
    assert data["n_selected"] == 2
    assert data["unclassified"] == 1  # 贵州茅台 money_class 为 None
    assert [row["money_class"] for row in data["by_class"]] == ["主力流入"]


# ---------------------------------------------------------------- 舆情


def test_news_highlights_traceable_to_source(full_store: Store):
    """每条舆情都要能回到原始链接与来源,否则不可追溯。"""
    section = _section(full_store_review(full_store), "news_highlights")
    assert section["available"] is True
    items = section["data"]["items"]
    assert len(items) == 2  # 只有当日两条,NEXT_DATE 那条不属于今天
    for item in items:
        assert item["url"].startswith("https://")
        assert item["published_at"]
        assert item["fetched_at"]
        assert item["source"]["source_id"] == "demo"
        assert item["source"]["name"] == "测试来源"
        assert item["judgement"]["label"] == LABEL_UNVERIFIED


def test_undecidable_sentiment_stays_none(full_store: Store):
    """判不出情绪就是 None。填 0 会被读成"中性",那是另一个结论。"""
    items = _section(full_store_review(full_store), "news_highlights")["data"]["items"]
    by_id = {item["news_id"]: item for item in items}
    assert by_id["n2"]["judgement"]["sentiment"] is None
    assert by_id["n2"]["judgement"]["sentiment_score"] is None
    assert by_id["n2"]["judgement"]["event_type"] is None
    assert by_id["n1"]["judgement"]["sentiment"] == "positive"
    assert by_id["n1"]["judgement"]["sentiment_score"] == pytest.approx(0.7)


def test_alignment_excludes_future_news(full_store: Store):
    """前视纪律:复盘 T 日不得读到 T+1 的新闻,否则是事后诸葛。"""
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_code = {row["ts_code"]: row for row in data["stocks"]}
    linked_ids = {item["news_id"] for item in by_code["601012.SH"]["news"]}
    assert linked_ids == {"n1"}
    assert "n3" not in linked_ids


def test_alignment_carries_match_basis(full_store: Store):
    """关联必须能说出"凭什么"。缺 match_basis 的关联不可用于研判。"""
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_code = {row["ts_code"]: row for row in data["stocks"]}
    link = by_code["601012.SH"]["news"][0]["link"]
    assert link["match_basis"] == "name_in_text"
    assert link["match_text"] == "隆基绿能"
    assert link["confidence"] == pytest.approx(0.9)


def test_alignment_news_keeps_full_source_info(full_store: Store):
    """关联舆情的来源信息不能比列表少。

    回归用:`Store.news_for_link` 的 SQL 一度漏选 `s.home_url AS source_home_url`,
    而 `_news_row()` 读的正是这个列名。缺列时 Series.get 返回 None 而不报错,
    来源主页会静默变成 null——按本文件其他断言的口径,null 意味着"缺失",
    于是"有主页"被讲成"没主页"。不抛异常,只能靠断言守住。
    """
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_code = {row["ts_code"]: row for row in data["stocks"]}
    source = by_code["601012.SH"]["news"][0]["source"]
    assert source["home_url"] == "https://example.com"
    assert source["name"] == "测试来源"


def test_alignment_pairs_price_with_news(full_store: Store):
    """价格、资金、舆情并排放,但不宣称因果。"""
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_code = {row["ts_code"]: row for row in data["stocks"]}
    assert by_code["601012.SH"]["pct_chg"] == pytest.approx(9.8)
    assert by_code["601012.SH"]["money_class"] == "主力流入"
    assert data["stocks_examined"] == 2  # 只看入选股票
    assert data["stocks_with_news"] == 1


def test_stock_without_news_says_so(full_store: Store):
    """没有关联舆情要明说,不能留一个空数组让人以为"查过了没消息"。"""
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_code = {row["ts_code"]: row for row in data["stocks"]}
    assert by_code["600519.SH"]["news"] == []
    assert by_code["600519.SH"]["news_missing_reason"] == "no_linked_news"
    assert by_code["601012.SH"]["news_missing_reason"] is None


def test_industry_alignment_included(full_store: Store):
    data = _section(full_store_review(full_store), "news_alignment")["data"]
    by_industry = {row["industry"]: row for row in data["industries"]}
    assert set(by_industry) == {"光伏设备", "白酒"}
    assert by_industry["光伏设备"]["news"][0]["news_id"] == "n1"
    assert by_industry["白酒"]["news_missing_reason"] == "no_linked_news"


# ---------------------------------------------------------------- 缺数据暴露


def test_no_source_registered_is_distinguishable(store: Store):
    """一个来源都没登记 = 链路未接入,不是"当天没有新闻"。"""
    _seed_scan(store)
    section = _section(
        build_review(store, trade_date=TRADE_DATE, strategy=STRATEGY), "news_highlights"
    )
    assert section["available"] is False
    assert section["missing_reason"] == "no_source_registered"
    assert section["data"] is None


def test_registered_but_never_collected(store: Store):
    """登记了来源却一条没采过,与"采过但当天没有"是两种状态。"""
    _seed_scan(store)
    store.upsert_news_sources(
        pd.DataFrame(
            [
                {
                    "source_id": "demo",
                    "name": "测试来源",
                    "kind": "news",
                    "home_url": "https://example.com",
                    "base_credibility": 0.9,
                    "compliance_note": "测试来源",
                    "enabled": True,
                }
            ]
        )
    )
    section = _section(
        build_review(store, trade_date=TRADE_DATE, strategy=STRATEGY), "news_highlights"
    )
    assert section["missing_reason"] == "never_collected"


def test_collected_but_none_on_that_day(store: Store):
    """采过、但复盘日当天没有条目——这才是真正的"今天没消息"。"""
    _seed_scan(store)
    _seed_news(store)
    section = _section(
        build_review(store, trade_date=PREV_DATE, strategy=STRATEGY), "news_highlights"
    )
    assert section["missing_reason"] == "no_news_on_date"
    assert TRADE_DATE in section["detail"] or NEXT_DATE in section["detail"]


def test_missing_scan_batch_marks_dependent_sections(store: Store):
    """没扫描批次时,依赖它的五节全部明确报缺,不返回空壳数据。"""
    _seed_news(store)
    review = build_review(store, trade_date=TRADE_DATE, strategy=STRATEGY)
    for key in (
        "industry_heat",
        "selection",
        "factor_contribution",
        "money_confirmation",
        "news_alignment",
    ):
        section = _section(review, key)
        assert section["available"] is False, key
        assert section["missing_reason"] == "no_scan_batch", key
        assert section["data"] is None, key
    # 不依赖扫描的两节照常可用
    assert _section(review, "market_structure")["available"] is True
    assert _section(review, "news_highlights")["available"] is True


def test_missing_snapshot_is_reported(store: Store):
    """本地没有该日行情时,市场结构报缺,而不是返回全 0 的"平静市场"。"""
    section = _section(
        build_review(store, trade_date="20250101", strategy=STRATEGY), "market_structure"
    )
    assert section["available"] is False
    assert section["missing_reason"] == "no_snapshot"


def test_missing_list_summarizes_gaps(store: Store):
    """缺数据要有一份汇总清单,页面不必逐节遍历才知道哪里空着。"""
    review = build_review(store, trade_date="20250101", strategy=STRATEGY)
    keys = {item["section"] for item in review["missing"]}
    assert "market_structure" in keys
    assert "news_highlights" in keys
    for item in review["missing"]:
        assert item["reason"] and item["detail"]
    assert set(review["available_sections"]).isdisjoint(keys)


def test_unknown_strategy_yields_no_batch(full_store: Store):
    """策略名不存在时明确报无批次,不静默回退到别的策略的结果。"""
    review = build_review(full_store, trade_date=TRADE_DATE, strategy="not_exist")
    assert _section(review, "selection")["missing_reason"] == "no_scan_batch"
    assert "not_exist" in _section(review, "selection")["detail"]


def test_wrong_date_yields_no_batch(full_store: Store):
    """扫描批次按 as_of 匹配:换一天就没有,不能拿最近一次批次顶替。"""
    review = build_review(full_store, trade_date=PREV_DATE, strategy=STRATEGY)
    assert _section(review, "selection")["missing_reason"] == "no_scan_batch"


def test_prediction_review_reports_pending(full_store: Store):
    """T+N 未到时属正常等待,要能与"数据缺失"区分开。"""
    picks = pd.DataFrame(
        [
            {
                "run_date": TRADE_DATE,
                "as_of": TRADE_DATE,
                "strategy": STRATEGY,
                "ts_code": "601012.SH",
                "name": "隆基绿能",
                "industry": "光伏设备",
                "rank": 1,
                "total": 0.91,
                "money_class": "主力流入",
                "one_line": "",
                "contrib_json": "{}",
                "feat_json": "{}",
                "ret1": None,
                "ret3": None,
                "ret5": None,
                "ret10": None,
            }
        ]
    )
    full_store.record_picks(picks)
    section = _section(
        build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY),
        "prediction_review",
    )
    assert section["available"] is True
    backfill = section["data"]["backfill"]
    # 默认只读:待回填量按列计数,但不给原因
    assert backfill["mode"] == "read_only"
    assert sum(backfill["pending"].values()) > 0
    # 原因要靠日历与行情逐条判定,是回填过程的产物;只读路径不许猜
    assert backfill["pending_reasons"] is None


def test_read_only_review_does_not_write(full_store: Store):
    """打开页面看复盘不该改库:只读模式下 retN 必须保持为空。"""
    picks = pd.DataFrame(
        [
            {
                "run_date": PREV_DATE,
                "as_of": PREV_DATE,
                "strategy": STRATEGY,
                "ts_code": "601012.SH",
                "name": "隆基绿能",
                "industry": "光伏设备",
                "rank": 1,
                "total": 0.91,
                "money_class": "主力流入",
                "one_line": "",
                "contrib_json": "{}",
                "feat_json": "{}",
                "ret1": None,
                "ret3": None,
                "ret5": None,
                "ret10": None,
            }
        ]
    )
    full_store.record_picks(picks)
    before = len(full_store.open_picks_awaiting_return("ret1"))
    build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    assert len(full_store.open_picks_awaiting_return("ret1")) == before


def test_backfill_true_explains_why_pending(full_store: Store):
    """显式要求回填时才给原因,且"未来还没到"要能与"缺数据"分开。"""
    picks = pd.DataFrame(
        [
            {
                "run_date": TRADE_DATE,
                "as_of": TRADE_DATE,
                "strategy": STRATEGY,
                "ts_code": "601012.SH",
                "name": "隆基绿能",
                "industry": "光伏设备",
                "rank": 1,
                "total": 0.91,
                "money_class": "主力流入",
                "one_line": "",
                "contrib_json": "{}",
                "feat_json": "{}",
                "ret1": None,
                "ret3": None,
                "ret5": None,
                "ret10": None,
            }
        ]
    )
    full_store.record_picks(picks)
    section = _section(
        build_review(
            full_store, trade_date=TRADE_DATE, strategy=STRATEGY, backfill=True
        ),
        "prediction_review",
    )
    reasons = section["data"]["backfill"]["pending_reasons"]
    assert reasons is not None
    # 夹具日历只到 20260803,T+3/T+5/T+10 的目标交易日尚未到达;
    # pending_reasons 按期限分桶(每桶内是原因->条数),把各桶加总再断言
    assert sum(sum(bucket.values()) for bucket in reasons.values()) > 0


def test_metadata_fields(full_store: Store):
    review = build_review(full_store, trade_date=TRADE_DATE, strategy=STRATEGY)
    assert review["trade_date"] == TRADE_DATE
    assert review["strategy"] == STRATEGY
    assert review["run_id"] == RUN_ID
    # 生成时间是墙钟时间,不能与交易日混为一谈
    assert review["generated_at"] != TRADE_DATE
    assert "T" in review["generated_at"]
