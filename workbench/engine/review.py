"""收盘后复盘装配。

把已入库的事实(行情、扫描明细、选股台账、舆情原文)组装成一份可直接
渲染的复盘结果。默认**只读**(`backfill=False`),也不做任何网络请求:
打开页面看一眼复盘不该悄悄改库。只有显式传 `backfill=True` 时才会顺带
回填 T+N 收益。

与 `postmortem.py` 的分工:后者负责回填 T+N 收益并算 IC / 胜率 / 分层,
是纯统计;本模块负责把统计与当天的市场结构、行业热度、入选淘汰、因子
贡献、资金确认、重要舆情拼成一份带标注的复盘,并把 `run_postmortem`
的输出作为"历史预测回看"一节嵌进来。

三类标注(criterion:结论必须区分事实、规则计算结果和待验证判断):

- fact       事实。直接来自已入库的行情/公告/新闻原文,不含判断。
- derived    规则计算结果。由固定公式或阈值从事实推出——换公式结论就会变。
- unverified 待验证判断。尚未被后续行情或人工确认,不能当结论用。

缺数据的处理:每一节要么 available=True 带 data,要么 available=False 带
missing_reason。**绝不用 0、空列表或占位文本冒充"已计算"**——"当天没有
重要舆情"和"舆情根本没采过"在页面上必须是两句不同的话。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from .db import Store
from .postmortem import HORIZONS, evaluate, run_postmortem, stats_as_dict

logger = logging.getLogger(__name__)

LABEL_FACT = "fact"
LABEL_DERIVED = "derived"
LABEL_UNVERIFIED = "unverified"

LABEL_LEGEND = {
    LABEL_FACT: "事实:直接来自已入库的行情、公告或新闻原文,不含任何判断",
    LABEL_DERIVED: "规则计算结果:由固定公式或阈值从事实推出,换公式结论就会变",
    LABEL_UNVERIFIED: "待验证判断:尚未被后续行情或人工确认,不可直接当结论使用",
}

# 复盘展示口径。放成常量而不是散在函数里,页面与测试共用同一份数字。
NEWS_HIGHLIGHT_LIMIT = 20
ALIGNMENT_STOCK_LIMIT = 20
ALIGNMENT_NEWS_LIMIT = 5
ALIGNMENT_INDUSTRY_LIMIT = 5
TOP_FACTOR_LIMIT = 12
TOP_REASON_LIMIT = 10
NEAR_LIMIT_UP_PCT = 9.5  # 近涨停阈值:实时源无涨停标志,用涨幅近似,属规则口径


# ---------------------------------------------------------------- 小工具


def _num(value: Any, digits: int = 4) -> Optional[float]:
    """数值规整。None / NaN 一律返回 None——缺数据就是缺,不拿 0 顶替。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def _int(value: Any) -> Optional[int]:
    """整数规整。计数与名次不该以 12.0 的形式出现在页面上。"""
    number = _num(value, 0)
    return None if number is None else int(number)


def _text(value: Any) -> Optional[str]:
    """文本规整。NaN / 空串一律返回 None,页面据此显示"未判定"。"""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    return text or None


def _json_obj(raw: Any, default: Any) -> Any:
    """解析入库时序列化的 JSON 列。解析失败不静默吞,记日志并回退。"""
    text = _text(raw)
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        logger.warning("复盘:JSON 列解析失败,已按缺失处理: %.80s", text)
        return default


def _flags(series: pd.Series) -> pd.Series:
    """布尔列规整。DuckDB 取回可能是 object/None,统一成 bool。"""
    return series.fillna(False).astype(bool)


def _available(title: str, label: str, data: Any, *, note: Optional[str] = None) -> dict:
    return {
        "title": title,
        "label": label,
        "available": True,
        "data": data,
        "note": note,
    }


def _missing(title: str, label: str, reason: str, detail: str) -> dict:
    """缺数据的一节。reason 供前端分支,detail 供人读。"""
    return {
        "title": title,
        "label": label,
        "available": False,
        "data": None,
        "missing_reason": reason,
        "detail": detail,
    }


# ---------------------------------------------------------------- 各节


def _market_structure(snap: pd.DataFrame, trade_date: str) -> dict:
    """当天市场结构:涨跌家数与成交额,全部是对已入库行情的计数与求和。"""
    if snap.empty:
        return _missing(
            "市场结构",
            LABEL_FACT,
            "no_snapshot",
            f"{trade_date} 本地无截面行情,请先更新市场数据",
        )
    pct = pd.to_numeric(snap["pct_chg"], errors="coerce")
    graded = pct.dropna()
    amount = pd.to_numeric(snap["amount"], errors="coerce")
    data = {
        "trade_date": trade_date,
        "total_symbols": int(len(snap)),
        "quoted_symbols": int(graded.size),
        # 有行无涨跌幅的票要单独报出来,否则"上涨家数"的分母会被悄悄改小
        "missing_pct_chg": int(len(snap) - graded.size),
        "up": int((graded > 0).sum()),
        "down": int((graded < 0).sum()),
        "flat": int((graded == 0).sum()),
        "near_limit_up": int((graded >= NEAR_LIMIT_UP_PCT).sum()),
        "near_limit_up_threshold": NEAR_LIMIT_UP_PCT,
        "avg_pct_chg": _num(graded.mean()),
        "median_pct_chg": _num(graded.median()),
        "total_amount": _num(amount.sum(), 2),
    }
    return _available(
        "市场结构",
        LABEL_FACT,
        data,
        note=(
            "涨跌家数与成交额为已入库行情的直接计数;"
            f"near_limit_up 用涨幅 ≥{NEAR_LIMIT_UP_PCT}% 近似,属规则口径而非交易所涨停标志"
        ),
    )


def _industry_heat(run: pd.Series) -> dict:
    """行业热度。heat 是加权公式的产物,换权重排序就会变,故标 derived。"""
    rows = _json_obj(run.get("top_industries_json"), [])
    if not rows:
        return _missing(
            "行业热度",
            LABEL_DERIVED,
            "no_industry_heat",
            "该批次未记录行业热度(top_industries_json 为空)",
        )
    data = [
        {
            "industry": _text(row.get("industry")),
            "count": _int(row.get("count")),
            "avg_pct": _num(row.get("avg_pct")),
            "med_pct": _num(row.get("med_pct")),
            "up_ratio": _num(row.get("up_ratio")),
            "strong_ratio": _num(row.get("strong_ratio")),
            "total_amount": _num(row.get("total_amount"), 2),
            "heat": _num(row.get("heat")),
        }
        for row in rows
    ]
    return _available(
        "行业热度",
        LABEL_DERIVED,
        data,
        note="heat = 0.25·均涨 + 0.25·中位涨 + 5·上涨占比 + 10·强势占比 + log1p(成交额)/5",
    )


def _selection(rows: pd.DataFrame) -> dict:
    """入选与淘汰。个股标识是事实,但入选与否由门槛与排名规则决定。"""
    if rows.empty:
        return _missing(
            "入选与淘汰", LABEL_DERIVED, "no_scan_rows", "该批次没有扫描明细行"
        )
    selected_mask = _flags(rows["selected"])
    passed_mask = _flags(rows["passed"])

    selected = [
        {
            "ts_code": _text(row["ts_code"]),
            "name": _text(row["name"]),
            "industry": _text(row["industry"]),
            "rank": _int(row["rank"]),
            "total": _num(row["total"]),
            "money_class": _text(row["money_class"]),
            "one_line": _text(row["one_line"]),
        }
        for _, row in rows[selected_mask].iterrows()
    ]

    # 淘汰原因按门槛条目计数:一只票可能同时踩多条,故总和会大于股票数
    reasons: dict[str, int] = {}
    for raw in rows[~passed_mask]["gate_reasons_json"]:
        for reason in _json_obj(raw, []):
            key = _text(reason)
            if key:
                reasons[key] = reasons.get(key, 0) + 1
    top_reasons = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)

    data = {
        "scored": int(len(rows)),
        "passed": int(passed_mask.sum()),
        "selected": int(selected_mask.sum()),
        # 过了门槛却没进最终名单的,是被排名截断而不是被规则否掉,两者要分开
        "passed_not_selected": int((passed_mask & ~selected_mask).sum()),
        "rejected": int((~passed_mask).sum()),
        "selected_list": selected,
        "reject_reasons": [
            {"reason": key, "count": count} for key, count in top_reasons[:TOP_REASON_LIMIT]
        ],
        "reject_reason_kinds": len(reasons),
    }
    return _available(
        "入选与淘汰",
        LABEL_DERIVED,
        data,
        note="淘汰原因按门槛条目计数,一只票可同时命中多条,合计会大于被否股票数",
    )


def _factor_contribution(rows: pd.DataFrame) -> dict:
    """因子贡献:入选股票上各因子对总分的平均贡献。"""
    selected = rows[_flags(rows["selected"])] if not rows.empty else rows
    if selected.empty:
        return _missing(
            "因子贡献",
            LABEL_DERIVED,
            "no_selected_rows",
            "该批次没有入选股票,无法统计因子贡献",
        )

    def _mean_of(column: str) -> list[dict]:
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for raw in selected[column]:
            for key, value in (_json_obj(raw, {}) or {}).items():
                number = _num(value, 6)
                if number is None:
                    continue
                totals[key] = totals.get(key, 0.0) + number
                counts[key] = counts.get(key, 0) + 1
        items = [
            {"key": key, "avg": _num(total / counts[key]), "n": counts[key]}
            for key, total in totals.items()
        ]
        return sorted(items, key=lambda item: item["avg"] or 0.0, reverse=True)

    by_factor = _mean_of("contrib_json")
    if not by_factor:
        return _missing(
            "因子贡献",
            LABEL_DERIVED,
            "no_contribution_recorded",
            "入选股票未记录因子贡献明细(contrib_json 为空)",
        )
    data = {
        "n_selected": int(len(selected)),
        "by_factor": by_factor[:TOP_FACTOR_LIMIT],
        "by_category": _mean_of("cat_scores_json"),
    }
    return _available(
        "因子贡献",
        LABEL_DERIVED,
        data,
        note="贡献 = 类别权重 × 归一化因子值,仅对入选股票取均值;不代表未来收益",
    )


def _money_confirmation(rows: pd.DataFrame) -> dict:
    """资金确认:入选股票的资金判定分布。未判定单列,不并进任何一类。"""
    selected = rows[_flags(rows["selected"])] if not rows.empty else rows
    if selected.empty:
        return _missing(
            "资金确认",
            LABEL_DERIVED,
            "no_selected_rows",
            "该批次没有入选股票,无法统计资金确认",
        )
    counts: dict[str, int] = {}
    unclassified = 0
    for value in selected["money_class"]:
        key = _text(value)
        if key is None:
            unclassified += 1
        else:
            counts[key] = counts.get(key, 0) + 1
    data = {
        "n_selected": int(len(selected)),
        "by_class": [
            {"money_class": key, "count": count}
            for key, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        # 资金流数据缺失就是缺失:不能默默算成"资金未确认",两者含义不同
        "unclassified": unclassified,
    }
    return _available(
        "资金确认",
        LABEL_DERIVED,
        data,
        note="资金判定来自 moneyflow 净额规则;unclassified 表示当日缺资金流数据,非'资金未确认'",
    )


def _news_highlights(store: Store, trade_date: str) -> dict:
    """当日重要舆情。原文字段是事实,情绪与事件分类是待验证判断。"""
    sources = store.news_sources()
    if sources.empty:
        return _missing(
            "重要舆情",
            LABEL_FACT,
            "no_source_registered",
            "尚未登记任何舆情来源,采集链路未接入(不同于'当天没有新闻')",
        )
    earliest, latest = store.news_date_range()
    if earliest is None:
        return _missing(
            "重要舆情",
            LABEL_FACT,
            "never_collected",
            f"已登记 {len(sources)} 个来源,但一条舆情都没采过",
        )

    items = store.news_by_trade_date(trade_date, limit=NEWS_HIGHLIGHT_LIMIT)
    if items.empty:
        return _missing(
            "重要舆情",
            LABEL_FACT,
            "no_news_on_date",
            f"舆情库覆盖 {earliest}~{latest},但 {trade_date} 当天没有条目",
        )

    data = {
        "coverage": {"earliest": earliest, "latest": latest},
        "items": [_news_row(row) for _, row in items.iterrows()],
    }
    return _available(
        "重要舆情",
        LABEL_FACT,
        data,
        note="标题、时间、链接为原文事实;judgement 一节是待验证判断,值为 null 表示未判定",
    )


def _news_row(row: pd.Series) -> dict:
    """单条舆情。事实与判断分开放,页面不必猜哪个字段能当结论用。"""
    return {
        "news_id": _text(row.get("news_id")),
        "title": _text(row.get("title")),
        "summary": _text(row.get("summary")),
        "url": _text(row.get("url")),
        "published_at": _text(row.get("published_at")),
        "fetched_at": _text(row.get("fetched_at")),
        "trade_date": _text(row.get("trade_date")),
        "source": {
            "source_id": _text(row.get("source_id")),
            "name": _text(row.get("source_name")),
            "kind": _text(row.get("source_kind")),
            "home_url": _text(row.get("source_home_url")),
            "base_credibility": _num(row.get("base_credibility")),
        },
        "judgement": {
            "label": LABEL_UNVERIFIED,
            "event_type": _text(row.get("event_type")),
            "sentiment": _text(row.get("sentiment")),
            "sentiment_score": _num(row.get("sentiment_score")),
            "credibility": _num(row.get("credibility")),
        },
    }


def _news_alignment(
    store: Store, rows: pd.DataFrame, snap: pd.DataFrame, trade_date: str
) -> dict:
    """舆情与价格/资金的对应关系。

    同一天既有消息又有涨幅,不等于消息导致了涨幅。这一节只把两边并排放,
    结论留给人,因此整节标 unverified。

    读舆情时保留 as_of=trade_date 的前视过滤:复盘 T 日却读到 T+1 的新闻,
    "舆情解释了走势"就变成了事后诸葛。
    """
    selected = rows[_flags(rows["selected"])] if not rows.empty else rows
    if selected.empty:
        return _missing(
            "舆情与价格资金对应",
            LABEL_UNVERIFIED,
            "no_selected_rows",
            "该批次没有入选股票,无法建立对应关系",
        )

    pct_by_code: dict[str, Any] = {}
    if not snap.empty:
        pct_by_code = dict(zip(snap["ts_code"], snap["pct_chg"]))

    stocks: list[dict] = []
    linked = 0
    for _, row in selected.head(ALIGNMENT_STOCK_LIMIT).iterrows():
        ts_code = _text(row["ts_code"])
        if ts_code is None:
            continue
        news = store.news_for_link(
            link_type="stock",
            link_key=ts_code,
            as_of=trade_date,
            limit=ALIGNMENT_NEWS_LIMIT,
        )
        if not news.empty:
            linked += 1
        stocks.append(
            {
                "ts_code": ts_code,
                "name": _text(row["name"]),
                "industry": _text(row["industry"]),
                "pct_chg": _num(pct_by_code.get(ts_code)),
                "money_class": _text(row["money_class"]),
                "news": [_linked_news_row(item) for _, item in news.iterrows()],
                "news_missing_reason": None if not news.empty else "no_linked_news",
            }
        )

    industries: list[dict] = []
    seen: set[str] = set()
    for value in selected["industry"]:
        name = _text(value)
        if name is None or name in seen:
            continue
        seen.add(name)
        if len(seen) > ALIGNMENT_INDUSTRY_LIMIT:
            break
        news = store.news_for_link(
            link_type="industry",
            link_key=name,
            as_of=trade_date,
            limit=ALIGNMENT_NEWS_LIMIT,
        )
        industries.append(
            {
                "industry": name,
                "news": [_linked_news_row(item) for _, item in news.iterrows()],
                "news_missing_reason": None if not news.empty else "no_linked_news",
            }
        )

    data = {
        "stocks": stocks,
        "industries": industries,
        "stocks_with_news": linked,
        "stocks_examined": len(stocks),
    }
    return _available(
        "舆情与价格资金对应",
        LABEL_UNVERIFIED,
        data,
        note=(
            "同日并列出现不构成因果;舆情已按 as_of 截断,不含复盘日之后发布的内容。"
            "每条关联都带 match_basis,说明'为什么认为这条新闻和这只股票有关'"
        ),
    )


def _linked_news_row(row: pd.Series) -> dict:
    """带关联依据的舆情条目。match_basis 缺失说明关联不可追溯,必须显式暴露。"""
    item = _news_row(row)
    item["link"] = {
        "match_basis": _text(row.get("match_basis")),
        "match_text": _text(row.get("match_text")),
        "confidence": _num(row.get("link_confidence")),
    }
    return item


def _prediction_review(
    store: Store, strategy: Optional[str], *, backfill: bool
) -> dict:
    """历史预测回看。IC / 胜率 / 分层都是样本内统计,标 derived。

    backfill=True 时顺带把能算的 retN 补上(调 run_postmortem);False 时只读,
    待回填量按列计数但**不给原因**——原因要靠日历与行情逐条判定,那是回填
    过程的产物,读路径凭空推断出来的"原因"是猜的。
    """
    if backfill:
        report = run_postmortem(store, strategy)
        # 补一个 mode 标记:页面据此区分"这份 pending 有原因"和"只读没判定"
        report.setdefault("backfill", {})["mode"] = "backfilled"
    else:
        report = {
            "backfill": {
                "mode": "read_only",
                "pending": {
                    col: int(len(store.open_picks_awaiting_return(col)))
                    for col in HORIZONS
                },
                # 只读模式下不判定原因:是"未来还没到"还是"缺行情",
                # 要走一遍日历与行情才知道,这里不猜。
                "pending_reasons": None,
            },
            "stats": [stats_as_dict(s) for s in evaluate(store, strategy)],
        }
    backfill_info = report.get("backfill", {})
    stats = report.get("stats", [])
    if not stats and not any((backfill_info.get("pending") or {}).values()):
        return _missing(
            "历史预测回看",
            LABEL_DERIVED,
            "no_picks_history",
            "选股台账还没有可回看的批次",
        )
    return _available(
        "历史预测回看",
        LABEL_DERIVED,
        report,
        note=(
            "IC / 胜率 / 分层均为样本内统计,不是未来收益承诺;"
            "pending_reasons 中 future_not_reached 属正常等待,其余为待处理的缺数据"
        ),
    )


# ---------------------------------------------------------------- 装配


def build_review(
    store: Store,
    *,
    trade_date: str,
    strategy: Optional[str] = None,
    backfill: bool = False,
) -> dict:
    """装配一份带标注的收盘后复盘结果(可直接 JSON 序列化)。

    参数:
        trade_date: 复盘目标交易日 YYYYMMDD。
        strategy:   策略名;None 表示不限定策略,取该日最先匹配到的批次。
        backfill:   是否顺带回填 T+N 收益。默认 False——页面查看复盘不该
                    悄悄改库。盘后链条已在自己的回填步做过,也传 False。

    任何一节缺数据都会以 available=False + missing_reason 返回,不抛异常;
    但底层数据库错误照常上抛——"读不到"和"读出来是空的"是两回事。
    """
    snap = store.snapshot(trade_date)
    runs = store.scan_runs(strategy)
    batch = runs[runs["as_of"] == trade_date] if not runs.empty else runs

    sections: dict[str, dict] = {"market_structure": _market_structure(snap, trade_date)}

    if batch.empty:
        detail = (
            f"{trade_date} "
            + (f"策略 {strategy} " if strategy else "")
            + "没有已记录的扫描批次,请先执行扫描"
        )
        for key, title, label in (
            ("industry_heat", "行业热度", LABEL_DERIVED),
            ("selection", "入选与淘汰", LABEL_DERIVED),
            ("factor_contribution", "因子贡献", LABEL_DERIVED),
            ("money_confirmation", "资金确认", LABEL_DERIVED),
            ("news_alignment", "舆情与价格资金对应", LABEL_UNVERIFIED),
        ):
            sections[key] = _missing(title, label, "no_scan_batch", detail)
        run_id = None
        batch_strategy = strategy
    else:
        run = batch.iloc[0]
        run_id = _text(run.get("run_id"))
        batch_strategy = _text(run.get("strategy")) or strategy
        rows = store.scan_rows(run_id) if run_id else pd.DataFrame()
        sections["industry_heat"] = _industry_heat(run)
        sections["selection"] = _selection(rows)
        sections["factor_contribution"] = _factor_contribution(rows)
        sections["money_confirmation"] = _money_confirmation(rows)
        sections["news_alignment"] = _news_alignment(store, rows, snap, trade_date)

    sections["news_highlights"] = _news_highlights(store, trade_date)
    sections["prediction_review"] = _prediction_review(
        store, batch_strategy, backfill=backfill
    )

    missing = [
        {"section": key, "reason": section["missing_reason"], "detail": section["detail"]}
        for key, section in sections.items()
        if not section["available"]
    ]
    return {
        "trade_date": trade_date,
        "strategy": batch_strategy,
        "run_id": run_id,
        # 生成时间是墙钟时间,与数据所属交易日无关,分开存避免被当成数据时间
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label_legend": LABEL_LEGEND,
        "sections": sections,
        "missing": missing,
        "available_sections": [k for k, s in sections.items() if s["available"]],
    }


def _cli() -> None:
    """命令行查看某个交易日的复盘结果。

    用法:
        python -m engine.review --trade-date 20260731
        python -m engine.review --trade-date 20260731 --strategy strong_mainup
    """
    import argparse

    from .config import load_settings, resolve_path

    parser = argparse.ArgumentParser(description="查看某交易日的收盘后复盘结果")
    parser.add_argument("--trade-date", required=True, help="交易日 YYYYMMDD")
    parser.add_argument("--strategy", default=None, help="策略名,默认不限定")
    parser.add_argument("--db", default=None, help="DuckDB 路径,默认取 settings.data.db_path")
    args = parser.parse_args()

    settings = load_settings()
    db_path = args.db or str(resolve_path(settings["data"]["db_path"]))
    with Store(db_path, ensure_schema=False) as store:
        review = build_review(store, trade_date=args.trade_date, strategy=args.strategy)
    print(json.dumps(review, ensure_ascii=False, indent=2, default=str))


__all__ = [
    "LABEL_DERIVED",
    "LABEL_FACT",
    "LABEL_LEGEND",
    "LABEL_UNVERIFIED",
    "build_review",
]


if __name__ == "__main__":
    _cli()
