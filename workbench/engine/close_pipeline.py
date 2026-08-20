"""收盘后任务链。

按固定顺序执行:更新市场数据 → 执行扫描 → 回填 T+N 收益 → 采集舆情 → 生成复盘。

三条硬规则:

1. **每步的结果独立记录**。链条返回的是一串 StepResult,而不是一个布尔值。
   任务失败时必须能看出"卡在哪一步、之前哪几步已经写库了",否则重跑时
   没人知道该从哪里接。
2. **失败即中止并上抛**。不做静默降级:摄取失败还继续扫描,会拿旧数据
   产出一份看起来正常的复盘,这比直接报错危险得多。
3. **不重复写入同一批次**。链条本身不判幂等——幂等由 task_runs 的业务键
   (kind, trade_date, strategy) 在调用方(PipelineManager)完成;链条内部各步
   依赖既有的 DELETE+INSERT 幂等写入(record_scan / record_picks / upsert)。

舆情采集依赖 settings.yaml 的 news 段。未配置任何启用来源时,该步明确返回
status="unavailable" 并说明原因,**不返回假数据、也不假装成功**;采集器抛错
则整步失败上抛,不降级成"今天没有舆情"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from .config import load_settings, tushare_token
from .db import Store
from .news import collect_news
from .news_config import build_fetchers, load_news_config
from .postmortem import backfill_returns
from .review import build_review
from .run_scan import run_scan
from .schedule import normalize_trade_date

logger = logging.getLogger(__name__)

# 链条步骤名(顺序即执行顺序),对外展示与前端进度条共用这一份口径
STEP_INGEST = "ingest_market"
STEP_SCAN = "scan"
STEP_BACKFILL = "backfill_returns"
STEP_NEWS = "collect_news"
STEP_POSTMORTEM = "postmortem"

PIPELINE_STEPS = (STEP_INGEST, STEP_SCAN, STEP_BACKFILL, STEP_NEWS, STEP_POSTMORTEM)

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StepResult:
    """单步结果。

    status:
        ok          —— 正常完成
        skipped     —— 按配置有意跳过(例如离线模式不做行情摄取)
        unavailable —— 依赖不具备,功能确实没执行(例如舆情采集器未接入)
    失败不用 status 表示:失败会直接抛异常中止链条。
    """

    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class PipelineResult:
    """整条链的结果。trade_date 是真实写入的批次日期。"""

    trade_date: str
    strategy: str
    online: bool
    steps: list[StepResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "strategy": self.strategy,
            "online": self.online,
            "steps": [s.as_dict() for s in self.steps],
            "unavailable_steps": [
                s.name for s in self.steps if s.status == STATUS_UNAVAILABLE
            ],
        }


def run_close_pipeline(
    *,
    db_path: str,
    strategy: str,
    trade_date: str,
    online: bool,
    exchange: str = "SSE",
    on_step: Optional[Callable[[StepResult], None]] = None,
) -> PipelineResult:
    """执行一次完整的收盘后任务链。

    参数:
        db_path: 目标 DuckDB 文件。测试必须传临时库。
        strategy: 扫描策略名。
        trade_date: 闸门判定出的目标交易日(YYYYMMDD),就是本次扫描的截面日。
        online: 是否联网更新行情。False 时跳过摄取,只跑本地闭环。
        on_step: 每步完成后的回调,用于刷心跳与落进度。回调异常会中止链条
                 (心跳写不进去说明库有问题,继续跑只会掩盖故障)。

    返回 PipelineResult;任一步失败直接上抛异常。

    trade_date 就是扫描截面:它由调用方按防前视口径算出(CLI 走
    require_visible_as_of)。必须原样传给 run_scan——run_scan 不收 as_of 时
    会自己取"最新交易日",那是隐藏窗口里的日期,等于绕过整条防前视闸门。
    """
    target_date = normalize_trade_date(trade_date)
    result = PipelineResult(trade_date=target_date, strategy=strategy, online=online)

    def _emit(step: StepResult) -> None:
        result.steps.append(step)
        logger.info("盘后任务链 [%s] %s: %s", step.name, step.status, step.detail)
        if on_step is not None:
            on_step(step)

    settings = load_settings()

    # ---------------------------------------------------- 1. 更新市场数据
    # 在线模式下 run_scan 内部已完成日历与快照摄取(confirm_latest_trade_date +
    # ingest_calendar + ingest_snapshot),这里不重复拉一遍,只记录本次的摄取口径。
    # 重复摄取除了浪费 Tushare 额度,还会让两次拉取之间的口径出现差异。
    if online:
        # 不在这里因为环境变量缺失就拒绝运行:TushareClient 是
        # `ts.pro_api(token) if token else ts.pro_api()`,没有环境变量时 tushare
        # 会回退到落盘 token,链条照样能跑。凭据到底行不行只有真正请求时才知道,
        # 那时 run_scan 会带着 Tushare 的原始报错抛出来——比这里猜一个更准确。
        # 这里只记录凭据来源,方便事后解释这批数据是拿哪个 token 拉的。
        env_key = (settings.get("tushare", {}) or {}).get("token_env", "TUSHARE_TOKEN")
        token_source = "env" if tushare_token(settings) else "tushare_local"
        _emit(
            StepResult(
                name=STEP_INGEST,
                status=STATUS_OK,
                detail=(
                    f"在线模式:凭据来源 {token_source}"
                    + ("" if token_source == "env" else f"(环境变量 {env_key} 未设置,回退 tushare 落盘 token)")
                    + ",行情与日历随扫描一并更新"
                ),
                data={
                    "mode": "online",
                    "delegated_to": STEP_SCAN,
                    "token_source": token_source,
                },
            )
        )
    else:
        _emit(
            StepResult(
                name=STEP_INGEST,
                status=STATUS_SKIPPED,
                detail="离线模式:不联网摄取,使用本地已入库行情",
                data={"mode": "offline"},
            )
        )

    # ---------------------------------------------------- 2. 执行扫描
    # 截面日固定为闸门目标日。在线摄取照旧把行情与日历拉到最新(数据要新),
    # 但选股只看 target_date 及更早的数据。
    scan = run_scan(
        strategy_name=strategy,
        online=online,
        db_path=db_path,
        record=True,
        as_of=target_date,
    )
    actual_date = scan.as_of
    result.trade_date = actual_date
    _emit(
        StepResult(
            name=STEP_SCAN,
            status=STATUS_OK,
            detail=f"{actual_date} {strategy} 扫描完成,入选 {len(scan.final)} 只",
            data={
                "run_id": scan.run_id,
                "as_of": actual_date,
                "candidate_count": scan.candidate_count,
                "scored_count": scan.scored_count,
                "passed_count": scan.passed_count,
                "final_count": len(scan.final),
            },
        )
    )

    # ---------------------------------------------------- 3. 回填 T+N 收益
    # run_scan 在 online+record 时已内部回填过一次;这里再显式跑一次是幂等的
    # (只补 retN 为空且未来收盘价已入库的记录),目的是拿到本步自己的统计数字,
    # 让"这次链条补了多少条"可查,而不是藏在扫描步里。
    # visible_max 传截面日:隐藏窗口里的目标交易日记 future_not_visible,不回填。
    with Store(db_path, ensure_schema=True) as store:
        backfill = backfill_returns(store, exchange, visible_max=actual_date)
    needs_attention = backfill.needs_attention()
    _emit(
        StepResult(
            name=STEP_BACKFILL,
            status=STATUS_OK,
            detail=(
                f"回填 {backfill.total_filled()} 条"
                + (f",另有需处理的缺数据 {needs_attention}" if needs_attention else "")
            ),
            data={
                "filled": backfill.filled,
                "pending": backfill.pending,
                "pending_reasons": backfill.pending_reasons,
                "needs_attention": needs_attention,
                "visible_max": actual_date,
            },
        )
    )

    # ---------------------------------------------------- 4. 采集舆情
    _emit(
        _collect_news_step(
            db_path=db_path,
            trade_date=actual_date,
            exchange=exchange,
            settings=settings,
        )
    )

    # ---------------------------------------------------- 5. 生成复盘
    # 装配带标注的复盘(事实 / 规则计算结果 / 待验证判断)。这里传 backfill=False:
    # 第 3 步已经显式回填过一次,复盘再回填一遍纯属重复写库,统计数字也会分裂成
    # 两处。缺数据的小节以 available=False 返回而不抛异常,因此这一步的 detail 要
    # 把缺了哪几节说清楚,否则页面只会看到一句"复盘完成"。
    with Store(db_path, ensure_schema=True) as store:
        review = build_review(
            store, trade_date=actual_date, strategy=strategy, backfill=False
        )
    ready = len(review.get("available_sections", []))
    total = len(review.get("sections", {}))
    missing = review.get("missing", [])
    detail = f"复盘装配完成,{ready}/{total} 节有数据"
    if missing:
        detail += ";缺数据:" + "、".join(
            f"{item['section']}({item['reason']})" for item in missing
        )
    _emit(
        StepResult(
            name=STEP_POSTMORTEM,
            status=STATUS_OK,
            detail=detail,
            data=review,
        )
    )

    return result


def _collect_news_step(
    *, db_path: str, trade_date: str, exchange: str, settings: dict
) -> StepResult:
    """舆情采集步骤。

    三种结局,页面上必须能区分:

    - unavailable:news 段未启用,或一个启用来源都没有。功能确实没执行。
    - ok + fetched=0:采集器跑了,但这段窗口内没有新条目。这是真实的"今天没消息"。
    - 抛异常:采集器失败。整条链中止,绝不降级成上面两种。

    这里绝不写入占位记录:空的舆情表能诚实地表达"还没采",
    而一条假新闻会污染后续的情绪统计与页面展示。
    """
    config = load_news_config(settings)
    if not config.enabled:
        return StepResult(
            name=STEP_NEWS,
            status=STATUS_UNAVAILABLE,
            detail="舆情采集未启用(settings.yaml 的 news.enabled=false),本步未执行",
            data={"trade_date": trade_date, "reason": "news_disabled", "collected": 0},
        )

    enabled_sources = config.enabled_sources
    if not enabled_sources:
        return StepResult(
            name=STEP_NEWS,
            status=STATUS_UNAVAILABLE,
            detail=(
                f"已登记 {len(config.sources)} 个舆情来源,但没有一个处于启用状态;"
                "复盘中的舆情部分将显示为缺失,不使用占位数据"
            ),
            data={
                "trade_date": trade_date,
                "reason": "no_enabled_source",
                "collected": 0,
                "declared_sources": [s.source.source_id for s in config.sources],
            },
        )

    fetchers = build_fetchers(config)
    with Store(db_path, ensure_schema=True) as store:
        result = collect_news(
            store=store,
            trade_date=trade_date,
            fetchers=fetchers,
            exchange=exchange,
            close_cutoff=config.close_cutoff,
            half_life_days=config.half_life_days,
        )

    rejected = len(result.rejected)
    return StepResult(
        name=STEP_NEWS,
        status=STATUS_OK,
        detail=(
            f"采集 {result.fetched} 条,入库 {result.stored} 条"
            + (f",转载 {result.duplicates} 条" if result.duplicates else "")
            + (f",拒收 {rejected} 条" if rejected else "")
        ),
        data=result.as_dict(),
    )


def _cli() -> None:
    """命令行手动触发,便于离线演示一次完整闭环。

    用法:
        python -m engine.close_pipeline --offline
        python -m engine.close_pipeline --trade-date 20260731 --db /tmp/t.duckdb
    """
    import argparse
    import json

    from .config import resolve_path
    from .schedule import load_schedule_config
    from .visibility import (
        LookaheadBlocked,
        ensure_visible,
        require_visible_as_of,
        resolve_window,
    )

    parser = argparse.ArgumentParser(description="手动执行一次收盘后任务链")
    parser.add_argument("--strategy", default=None, help="策略名,默认取 schedule 配置")
    parser.add_argument(
        "--trade-date",
        default=None,
        help="目标交易日 YYYYMMDD,默认取可见日(基准日往前退 N 个开市日)",
    )
    parser.add_argument("--offline", action="store_true", help="离线模式,不联网摄取")
    parser.add_argument("--db", default=None, help="DuckDB 路径,默认取 settings.data.db_path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    config = load_schedule_config(settings)
    db_path = args.db or str(resolve_path(settings["data"]["db_path"]))
    strategy = args.strategy or config.strategy

    # 日期口径:默认日期只能是可见日,显式日期必须 <= 可见日。
    # 原来取"日历最近开市日"直接落在隐藏窗口里,等于拿还没落地的行情跑链条。
    with Store(db_path, ensure_schema=False) as store:
        window = resolve_window(store, settings, exchange=config.exchange)
    if args.trade_date is None:
        try:
            trade_date = require_visible_as_of(window)
        except LookaheadBlocked as exc:
            raise SystemExit(f"无法确定目标交易日:{exc}") from exc
    else:
        try:
            trade_date = ensure_visible(
                normalize_trade_date(args.trade_date), window
            )
        except LookaheadBlocked as exc:
            raise SystemExit(f"拒绝执行:{exc}") from exc

    result = run_close_pipeline(
        db_path=db_path,
        strategy=strategy,
        trade_date=trade_date,
        online=not args.offline,
        exchange=config.exchange,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
