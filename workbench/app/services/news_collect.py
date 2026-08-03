""""只采舆情"一键触发的应用层管理器。

与 `PipelineManager` 的分工:盘后任务链跑整条(摄取→扫描→回填→舆情→复盘),
本模块只跑其中的**舆情采集**一步,便于单独调试与手动补采,不牵动扫描与复盘。

为什么单独一层而不是塞进只读的 `NewsService`:
    打开情绪页是只读操作,绝不该触发网络抓取。采集是有副作用的后台动作,
    必须走 task_runs 落库、幂等、失败显式上抛——这套东西和 PipelineManager
    一致,所以照它的三条约定办:

1. **"已完成"不是错误**。同一交易日已采成功,返回既有任务详情并带 reused=True,
   由路由层决定 200 还是 202,不走错误信封。
2. **抢占失败必须带回冲突行**。没带回来是存储层契约被破坏,直接 500 暴露。
3. **失败先落库再原样上抛**。task_runs 里留下 failed 与错误详情,日志留完整堆栈,
   绝不静默吞掉。

舆情采集没有"策略"维度,幂等键的 strategy 段固定留空("")。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from engine.config import load_settings
from engine.db import Store
from engine.news import collect_news
from engine.news_config import NewsConfig, NewsConfigError, build_fetchers, load_news_config
from engine.schedule import (
    ScheduleConfigError,
    is_trading_day,
    load_schedule_config,
    normalize_trade_date,
)

from app.errors import WorkbenchError
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "news_collect"

# 只采一步,比整条链短得多。11 个热榜平台各自最多重试两次、每次 10s 超时,
# 最坏也就几分钟;30 分钟阈值只用来兜住进程被杀、连心跳都停了的僵死任务。
STALE_AFTER_SECONDS = 1800

# 舆情采集无策略维度,幂等键的 strategy 段固定留空。
_NO_STRATEGY = ""


class NewsCollectManager:
    """舆情采集管理器。单工作线程,保证同进程内不并发采集。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.tracker = TaskTracker(self.db_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="quant-news"
        )

    # ------------------------------------------------------------ 配置
    def _news_config(self) -> NewsConfig:
        """读取舆情配置。非法直接 400 暴露,不回退默认值。"""
        try:
            return load_news_config(load_settings())
        except NewsConfigError as exc:
            raise WorkbenchError(
                "news_config_invalid", str(exc), status_code=400
            ) from exc

    def _exchange(self) -> str:
        """采集归属交易日用的日历口径,与盘后链保持一致(settings.schedule.exchange)。"""
        try:
            return load_schedule_config(load_settings()).exchange
        except ScheduleConfigError as exc:
            raise WorkbenchError(
                "schedule_config_invalid", str(exc), status_code=400
            ) from exc

    # ------------------------------------------------------------ 启动
    def start(
        self,
        *,
        trade_date: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """提交一次舆情采集。

        参数:
            trade_date: 目标交易日(YYYYMMDD 或带横线)。省略时取日历最近开市日;
                给了就必须是开市日,否则 400——猜一个日期会把整批舆情挂错交易日。
            force: 绕过"已成功"检查,强制重采同一交易日。

        幂等:同一交易日已 succeeded -> 返回既有详情 + reused=True;
        有活跃任务 -> 409;僵死任务 -> 自动抢占重试。

        采集未启用或无启用来源时 409:这不是"采到 0 条",而是功能没开,
        必须与真实的"今天没热点"区分开。
        """
        config = self._news_config()
        if not config.enabled:
            raise WorkbenchError(
                "news_disabled",
                "舆情采集未启用(settings.yaml 的 news.enabled=false),无法触发采集",
                status_code=409,
            )
        if not config.enabled_sources:
            raise WorkbenchError(
                "no_enabled_source",
                f"已登记 {len(config.sources)} 个舆情来源,但没有一个处于启用状态",
                status_code=409,
                details={"declared_sources": [s.source.source_id for s in config.sources]},
            )

        target_date = self._resolve_trade_date(trade_date)
        claim = self.tracker.claim(
            kind=TASK_KIND,
            trade_date=target_date,
            strategy=_NO_STRATEGY,
            force=force,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )

        if not claim.claimed:
            return self._handle_conflict(claim, target_date)

        self._executor.submit(self._run, claim.task_id, target_date, config)
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND,
            "trade_date": target_date,
            "sources": [s.source.source_id for s in config.enabled_sources],
            "created_at": self.tracker.now(),
            "reused": False,
        }

    def _handle_conflict(self, claim, target_date: str) -> dict:
        """抢占失败的分支处理,语义与 PipelineManager 对齐。"""
        conflict = claim.conflict
        # 抢占失败必然带回冲突行;没带回来说明 Store 契约被破坏,直接暴露
        if conflict is None:
            raise WorkbenchError(
                "task_claim_inconsistent",
                "抢占任务失败但未返回冲突任务,存储层状态异常",
                status_code=500,
            )
        if conflict["status"] == "succeeded":
            # 已完成不是错误。重读整行拿权威值,而不是直接用冲突字典
            # (它不含 trade_date/kind,且完成时可能回写过真实 as_of)。
            existing = self.tracker.get(conflict["task_id"])
            if existing is None:
                raise WorkbenchError(
                    "task_claim_inconsistent",
                    f"冲突任务 {conflict['task_id']} 在库中不存在,存储层状态异常",
                    status_code=500,
                )
            existing["reused"] = True
            return existing
        raise WorkbenchError(
            "news_collect_in_progress",
            f"{target_date} 的舆情采集正在运行",
            status_code=409,
            details=conflict,
        )

    def _resolve_trade_date(self, trade_date: Optional[str]) -> str:
        """确定目标交易日。

        手动指定时校验它确实是开市日(不能让步,否则批次挂在非交易日上);
        省略时取日历最近的开市日,与 close_pipeline._cli 的口径一致。
        """
        exchange = self._exchange()
        if trade_date is not None:
            try:
                target = normalize_trade_date(trade_date)
            except ScheduleConfigError as exc:
                raise WorkbenchError(
                    "invalid_trade_date", str(exc), status_code=400
                ) from exc
            with Store(self.db_path, ensure_schema=False) as store:
                open_dates = store.open_dates(exchange, target, 1)
            if not open_dates:
                raise WorkbenchError(
                    "calendar_missing",
                    f"trade_cal 中没有 {exchange} 在 {target} 及之前的开市日记录,"
                    "无法确认它是交易日;请先回补交易日历",
                    status_code=503,
                )
            if not is_trading_day(target, open_dates):
                raise WorkbenchError(
                    "not_trading_day",
                    f"{target} 不是 {exchange} 的开市日,拒绝为它采集舆情",
                    status_code=400,
                )
            return target

        # 取"明天及之前"的最近开市日:当天盘中也能采,归属由 close_cutoff 决定。
        with Store(self.db_path, ensure_schema=False) as store:
            end = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
            dates = store.open_dates(exchange, end, 1)
        if not dates:
            raise WorkbenchError(
                "calendar_missing",
                f"trade_cal 无 {exchange} 开市日记录,无法确定目标交易日;请先回补日历",
                status_code=503,
            )
        return dates[-1]

    # ------------------------------------------------------------ 执行
    def _run(self, task_id: str, trade_date: str, config: NewsConfig) -> None:
        """后台线程执行采集。失败先落库再上抛,绝不静默吞掉。"""
        self.tracker.mark_running(task_id)
        try:
            fetchers = build_fetchers(config)
            with Store(self.db_path, ensure_schema=True) as store:
                result = collect_news(
                    store=store,
                    trade_date=trade_date,
                    fetchers=fetchers,
                    exchange=self._exchange(),
                    close_cutoff=config.close_cutoff,
                    half_life_days=config.half_life_days,
                )
        except Exception as error:  # noqa: BLE001 - 落库后原样上抛
            self.tracker.finish(
                task_id,
                status="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            logger.exception("舆情采集 %s(%s)失败", task_id, trade_date)
            raise

        self.tracker.finish(
            task_id,
            status="succeeded",
            result=result.as_dict(),
        )

    # ------------------------------------------------------------ 查询
    def get(self, job_id: str) -> dict:
        task = self.tracker.get(job_id)
        if task is None:
            raise WorkbenchError(
                "news_collect_job_not_found", "舆情采集任务不存在", status_code=404
            )
        return task

    def latest(self) -> Optional[dict]:
        return self.tracker.latest(kind=TASK_KIND)

    def recent(self, *, limit: int = 20) -> list[dict]:
        if limit <= 0:
            raise WorkbenchError("invalid_limit", "limit 必须为正整数", status_code=400)
        return self.tracker.recent(kind=TASK_KIND, limit=limit)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["NewsCollectManager", "TASK_KIND", "STALE_AFTER_SECONDS"]
