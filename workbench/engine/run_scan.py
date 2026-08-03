"""扫描入口:ingest → universe → context → score → 台账。

编排全链路,是旧脚本 under70_strict_mainup_scan.py 的引擎化替身:
1. 确认最新收盘交易日(本地优先,缺失则从 Tushare 拉齐)。
2. 构建候选池(硬过滤 + 行业热度 + 种子召回)。
3. 为每只候选构建 StockContext(含资金后置确认)。
4. 打分 + 门槛 + 行业去重 + 取 top_n。
5. 写 picks 台账(供事后复盘/IC 自检)。

前视纪律:所有历史、资金流查询按 <= as_of 过滤;资金流缺失置 NaN,不反填未来。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import load_settings, load_strategy, resolve_path, tushare_token
from .db import Store
from .factors.context import build_context, StockContext
from .factors.money import classify_money
from .ingest_tushare import (
    TushareClient,
    confirm_latest_trade_date,
    ingest_calendar,
    ingest_history,
    ingest_moneyflow,
    ingest_snapshot,
)
from .score import dedup_and_top, score_pool, ScoredStock
from .universe import (
    amount_top15pct_threshold,
    apply_universe,
    build_candidates,
    industry_heat,
    industry_meta,
)


@dataclass
class ScanResult:
    run_id: str
    as_of: str
    strategy: str
    candidate_count: int
    scored_count: int
    passed_count: int
    final: List[ScoredStock]
    scored: List[ScoredStock] = field(default_factory=list)
    top_industries: List[Dict[str, Any]] = field(default_factory=list)


def _make_client(settings: Dict[str, Any]) -> TushareClient:
    """构造 Tushare 客户端(延迟导入 tushare,避免无 token 环境导入失败)。"""
    token = tushare_token(settings)
    ts_cfg = settings.get("tushare", {}) or {}
    return TushareClient(
        token,
        retry=int(ts_cfg.get("retry", 3)),
        sleep=float(ts_cfg.get("sleep", 0.02)),
        timeout=int(ts_cfg.get("request_timeout_seconds", 120)),
    )


def _ensure_data(
    store: Store,
    settings: Dict[str, Any],
    *,
    online: bool,
    client: Optional[TushareClient],
) -> tuple[str, int]:
    """确认 as_of;online 时补齐截面/基础/日历。返回 (as_of, rows)。"""
    min_rows = int(settings["data"]["min_daily_rows"])
    local = store.latest_confirmed_date(min_rows)
    if not online:
        # 离线优先用已确认交易日;小样本(测试/回补中)回退到最大本地日期
        as_of = local or store.latest_date()
        if not as_of:
            raise RuntimeError("离线模式但本地无任何日线数据,请先联网 ingest。")
        return as_of, min_rows + 1

    assert client is not None
    as_of, rows = confirm_latest_trade_date(client, min_rows)
    start_cal = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    ingest_calendar(store, client, start_cal, as_of)
    ingest_snapshot(store, client, as_of)
    return as_of, rows


def _backfill_history(
    store: Store,
    client: TushareClient,
    cand: pd.DataFrame,
    as_of: str,
    bars: int,
) -> None:
    """为候选池回补足量日线(<= as_of),已有足量则跳过。"""
    open_dates = store.open_dates("SSE", as_of, bars + 20)
    start = open_dates[0] if open_dates else (
        (datetime.now() - timedelta(days=bars * 2)).strftime("%Y%m%d")
    )
    need: List[str] = []
    for code in cand["ts_code"].tolist():
        have = store.history(code, as_of, bars)
        if have is None or len(have) < bars * 0.8:
            need.append(code)
    if need:
        ingest_history(store, client, need, start, as_of)


def _backfill_moneyflow(
    store: Store,
    client: TushareClient,
    codes: List[str],
    as_of: str,
) -> None:
    open_dates = store.open_dates("SSE", as_of, 10)
    if not open_dates:
        return
    start = open_dates[0]
    need: List[str] = []
    for code in codes:
        have = store.moneyflow_tail(code, as_of, 5)
        if have is None or have.empty:
            need.append(code)
    if need:
        ingest_moneyflow(store, client, need, start, as_of)


def _money_class_for(
    store: Store, code: str, as_of: str
) -> tuple[Optional[str], Dict[str, float]]:
    """由本地 moneyflow 计算近5日资金分层。缺失返回 (None, {})。"""
    mf = store.moneyflow_tail(code, as_of, 5)
    if mf is None or mf.empty:
        return None, {}
    net5 = (
        float(mf["net_mf_amount"].astype(float).sum())
        if "net_mf_amount" in mf.columns
        else float("nan")
    )
    big_daily = (
        (mf["buy_lg_amount"].astype(float) - mf["sell_lg_amount"].astype(float))
        + (mf["buy_elg_amount"].astype(float) - mf["sell_elg_amount"].astype(float))
    )
    big5 = float(big_daily.sum())
    cls = classify_money(net5, big5)
    money = {
        "net5": net5,
        "big5": big5,
        "big_pos_days": float((big_daily > 0).sum()),
    }
    return cls, money


def _build_contexts(
    store: Store,
    cand: pd.DataFrame,
    heat_map: Dict[str, float],
    rank_map: Dict[str, int],
    top_inds: List[str],
    as_of: str,
    bars: int,
    price_max: Optional[float],
) -> List[StockContext]:
    amount_q85 = amount_top15pct_threshold(cand) if len(cand) else 0.0
    top8 = set(top_inds[:8])

    contexts: List[StockContext] = []
    for _, r in cand.iterrows():
        code = r["ts_code"]
        hist = store.history(code, as_of, bars)
        snap = {
            "pct_chg": r.get("pct_chg"),
            "amount": r.get("amount"),
            "turnover_rate": r.get("turnover_rate"),
            "volume_ratio": r.get("volume_ratio"),
        }
        industry = r.get("industry")
        ctx = build_context(
            ts_code=code,
            name=r.get("name"),
            industry=industry,
            as_of=as_of,
            hist=hist,
            snapshot=snap,
            industry_heat=float(heat_map.get(industry, 0.0)),
            industry_rank=int(rank_map.get(industry, 999)),
            amount_top15pct=bool(float(r.get("amount", 0) or 0) > amount_q85),
        )
        if ctx is None:
            continue
        # 单价上限二次确认(用确认收盘价)
        if price_max is not None and ctx.get("last_close") >= float(price_max):
            continue
        ctx.feat["industry_top8"] = 1.0 if industry in top8 else 0.0
        # 资金后置确认
        cls, money = _money_class_for(store, code, as_of)
        ctx.money_class = cls
        ctx.money = money
        for k, v in money.items():
            ctx.feat[k] = v
        contexts.append(ctx)
    return contexts


def run_scan(
    *,
    strategy_name: Optional[str] = None,
    online: bool = True,
    db_path: Optional[str] = None,
    record: bool = True,
    overrides: Optional[Dict[str, Any]] = None,
) -> ScanResult:
    """执行一次扫描并(可选)写入台账。

    overrides 允许运行时覆盖策略字段(供旧脚本 CLI 兼容层使用),支持:
    - price_max / min_amount_yi -> universe
    - top_n / candidate_limit
    """
    settings = load_settings()
    strat_name = strategy_name or settings["engine"]["default_strategy"]
    strat = load_strategy(strat_name)

    ov = dict(overrides or {})
    uni = dict(strat.get("universe", {}) or {})
    if ov.get("price_max") is not None:
        uni["price_max"] = float(ov["price_max"])
    if ov.get("min_amount_yi") is not None:
        uni["min_amount_yi"] = float(ov["min_amount_yi"])
    strat["universe"] = uni
    if ov.get("top_n") is not None:
        strat["top_n"] = int(ov["top_n"])

    dbp = db_path or str(resolve_path(settings["data"]["db_path"]))
    bars = int(settings["engine"].get("history_bars", 150))
    cand_limit = int(ov.get("candidate_limit") or settings["engine"].get("candidate_limit", 260))
    price_max = uni.get("price_max")

    client = _make_client(settings) if online else None

    with Store(dbp) as store:
        as_of, _rows = _ensure_data(store, settings, online=online, client=client)
        snap = store.snapshot(as_of)
        if snap.empty:
            raise RuntimeError(f"as_of={as_of} 本地无截面数据。")

        m = apply_universe(snap, strat.get("universe", {}) or {})
        ind = industry_heat(m)
        heat_map, rank_map, top_inds = industry_meta(ind)
        cand = build_candidates(m, ind, top_inds, cand_limit)

        if online and client is not None:
            _backfill_history(store, client, cand, as_of, bars)
            _backfill_moneyflow(store, client, cand["ts_code"].tolist(), as_of)

        contexts = _build_contexts(
            store, cand, heat_map, rank_map, top_inds, as_of, bars, price_max
        )
        scored = score_pool(contexts, strat)
        passed = [s for s in scored if s.passed]
        final = dedup_and_top(
            scored,
            max_per_industry=int((strat.get("dedup", {}) or {}).get("max_per_industry", 2)),
            top_n=int(strat.get("top_n", 6)),
            require_pass=True,
        )

        result = ScanResult(
            run_id=uuid.uuid4().hex,
            as_of=as_of,
            strategy=strat_name,
            candidate_count=int(len(cand)),
            scored_count=int(len(scored)),
            passed_count=int(len(passed)),
            final=final,
            scored=scored,
            top_industries=ind.head(15).to_dict(orient="records"),
        )

        if record:
            _record_scan(store, result)
            if final:
                _record_picks(store, result)

        # 自动复盘:在线模式下,顺带回填历史选股中"未来第N日已到"的收益。
        # 幂等——只补 retN 仍为空且本地已有未来收盘价的记录,不动其余。
        if online and record:
            from .postmortem import backfill_returns

            backfill_returns(store)

    return result


def _record_scan(store: Store, result: ScanResult) -> None:
    run_date = datetime.now().strftime("%Y%m%d%H%M%S")
    selected_codes = {stock.ts_code for stock in result.final}
    rows = []
    for rank, stock in enumerate(result.scored, start=1):
        rows.append({
            "run_id": result.run_id,
            "ts_code": stock.ts_code,
            "name": stock.name,
            "industry": stock.industry,
            "rank": rank,
            "total": float(stock.total),
            "passed": bool(stock.passed),
            "selected": stock.ts_code in selected_codes,
            "gate_reasons_json": json.dumps(stock.gate_reasons, ensure_ascii=False),
            "cat_scores_json": json.dumps(stock.cat_scores, ensure_ascii=False, default=str),
            "money_class": stock.money_class,
            "one_line": stock.one_line,
            "contrib_json": json.dumps(stock.contrib, ensure_ascii=False, default=str),
            "feat_json": json.dumps(stock.feat, ensure_ascii=False, default=str),
        })
    run_row = {
        "run_id": result.run_id,
        "run_date": run_date,
        "as_of": result.as_of,
        "strategy": result.strategy,
        "candidate_count": result.candidate_count,
        "scored_count": result.scored_count,
        "passed_count": result.passed_count,
        "final_count": len(result.final),
        "top_industries_json": json.dumps(
            result.top_industries, ensure_ascii=False, default=str
        ),
    }
    store.record_scan(run_row, pd.DataFrame(rows))


def _record_picks(store: Store, result: ScanResult) -> None:
    run_date = datetime.now().strftime("%Y%m%d")
    rows = []
    for rank, s in enumerate(result.final, start=1):
        rows.append({
            "run_date": run_date,
            "as_of": result.as_of,
            "strategy": result.strategy,
            "ts_code": s.ts_code,
            "name": s.name,
            "industry": s.industry,
            "rank": rank,
            "total": float(s.total),
            "money_class": s.money_class,
            "one_line": s.one_line,
            "contrib_json": json.dumps(s.contrib, ensure_ascii=False),
            "feat_json": json.dumps(s.feat, ensure_ascii=False, default=str),
            "ret1": None, "ret3": None, "ret5": None, "ret10": None,
        })
    store.record_picks(pd.DataFrame(rows))


def to_legacy_output(result: ScanResult, *, price_max: Optional[float]) -> Dict[str, Any]:
    """把 ScanResult 映射为旧脚本 under70_strict_mainup_scan.py 的 JSON 结构。

    旧消费方(skill/cron)读取 final[*] 的字段并做叙述,故此处按旧字段名回填,
    数据取自引擎 feat/contrib,保证下游零改动。
    """
    def _row(rank: int, s: ScoredStock) -> Dict[str, Any]:
        f = s.feat
        return {
            "ts_code": s.ts_code,
            "name": s.name,
            "industry": s.industry,
            "close": f.get("last_close"),
            "pct_chg": f.get("pct_chg"),
            "amount": f.get("amount"),
            "ret20": f.get("ret20"),
            "ret60": f.get("ret60"),
            "pos60": f.get("pos60"),
            "new20": bool(f.get("new20", 0)),
            "new60": bool(f.get("new60", 0)),
            "macd_dif": f.get("macd_dif"),
            "macd_dea": f.get("macd_dea"),
            "macd_hist": f.get("macd_hist"),
            "macd_bull": int(f.get("macd_bull", 0)),
            "weekly_bull": int(f.get("weekly_bull", 0)),
            "ma_stack": int(f.get("ma_stack", 0)),
            "vol5_20": f.get("vol5_20"),
            "vol20_60": f.get("vol20_60"),
            "amt5_20": f.get("amt5_20"),
            "vol_score": int(f.get("vol_health", 0)),
            "net5": f.get("net5"),
            "big5": f.get("big5"),
            "big_pos_days": int(f.get("big_pos_days", 0)) if f.get("big_pos_days") == f.get("big_pos_days") else 0,
            "money_class": s.money_class,
            "score": round(float(s.total), 4),
            "one_line": s.one_line,
            "rank": rank,
            "contrib": {k: round(v, 4) for k, v in s.contrib.items()},
        }

    final_rows = [_row(i + 1, s) for i, s in enumerate(result.final)]
    return {
        "latest_trade_date": result.as_of,
        "price_max": price_max,
        "hard_filters": [
            "close < price_max", "exclude ST/*ST", "exclude 300/301",
            "exclude 688/689", "exclude BSE-like 8/4/9 prefixes",
        ],
        "top_industries": result.top_industries,
        "candidate_count": result.candidate_count,
        "structured_count": result.scored_count,
        "passed_count": result.passed_count,
        "final": final_rows,
        "engine": "quant_workbench",
        "strategy": result.strategy,
    }


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Quant workbench 扫描入口")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--offline", action="store_true", help="仅用本地数据,不联网")
    ap.add_argument("--no-record", action="store_true", help="不写 picks 台账")
    ap.add_argument("--output", default=None, help="额外导出 JSON 结果")
    args = ap.parse_args()

    res = run_scan(
        strategy_name=args.strategy,
        online=not args.offline,
        record=not args.no_record,
    )
    summary = {
        "as_of": res.as_of,
        "strategy": res.strategy,
        "candidate_count": res.candidate_count,
        "scored_count": res.scored_count,
        "passed_count": res.passed_count,
        "final": [
            {"rank": i + 1, "ts_code": s.ts_code, "name": s.name,
             "industry": s.industry, "total": round(s.total, 4),
             "money_class": s.money_class, "one_line": s.one_line}
            for i, s in enumerate(res.final)
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    _cli()
