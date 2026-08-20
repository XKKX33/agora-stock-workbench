from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from engine.config import load_settings
from engine.db import Store
from engine.ingest_tushare import (
    confirm_latest_trade_date,
    ingest_calendar,
    ingest_snapshot,
)
from engine.run_scan import (
    ScanResult,
    _calendar_lookahead_end,
    _make_client,
    prepare_scan_data,
    score_prepared_scan,
    validate_scan_integrity,
)
from engine.visibility import (
    LookaheadBlocked,
    require_visible_as_of,
    resolve_window,
)

from app.errors import WorkbenchError
from app.services.tasks import DEFAULT_STALE_AFTER_SECONDS, TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "scan"

# 交易日历口径:全项目统一按上交所日历推可见窗口。
EXCHANGE = "SSE"

# 单次扫描比盘后任务链短,沿用 TaskTracker 的默认僵死阈值即可。
STALE_AFTER_SECONDS = DEFAULT_STALE_AFTER_SECONDS


class ScanManager:
    """扫描任务管理器,持久化到 task_runs 表,支持跨进程幂等与崩溃恢复。

    改动要点:
    - 不再用内存 dict 保存任务状态,全部写入 task_runs 表。
    - 经 TaskTracker 抢占业务幂等键 (kind="scan", trade_date=as_of, strategy)。
    - 已有 succeeded 记录时不重复扫描;已有僵死任务时自动抢占重试。
    - 服务重启后状态仍在库中,可继续查询历史任务。

    task_runs 的读写一律走 `TaskTracker`,不在这里另抄一份 JSON 解析与字段装饰:
    两份实现最终一定会漂移(一边补了字段另一边没补),而漂移的表现是页面上
    某类任务少一个字段,很难定位。与 `PipelineManager` 共用同一层。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.tracker = TaskTracker(self.db_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quant-scan")

    def start(self, *, strategy: str, online: bool, record: bool, force: bool = False) -> dict:
        """提交新扫描任务。返回任务元信息(job_id, status, trade_date, ...)。

        幂等:同一 (trade_date, strategy) 已 succeeded -> 返回已完成详情;
        已有活跃任务 -> 409;僵死任务 -> 自动抢占重试。

        force=True 时强制重跑,绕过 succeeded 检查。

        日期口径(可见闸门):扫描截面只能是可见日 = 基准日往前退 N 个开市日,
        不再让 run_scan 自己去确认"最新交易日"——那等于拿隐藏窗口里的行情选股,
        是最直接的前视偏差。这里用本地基准日预解析可见日作为抢占键;在线模式下
        Tushare 可能确认出更新的基准日,`_run()` 会用刷新后的基准日重算可见日,
        并在完成时把真实 as_of 回写到 task_runs.trade_date,使幂等键与真实批次
        对齐,后续同 as_of 的重跑仍能被拦住。

        窗口算不出来(没有基准日 / 日历没覆盖 / 历史开市日不足)一律 409 报错,
        绝不回退成最新交易日。
        """
        settings = load_settings()

        with Store(self.db_path, ensure_schema=False) as store:
            window = resolve_window(store, settings, exchange=EXCHANGE)
        try:
            trade_date = require_visible_as_of(window)
        except LookaheadBlocked as exc:
            raise WorkbenchError(
                exc.code,
                str(exc),
                status_code=409,
                details=window.as_dict(),
            ) from exc

        claim = self.tracker.claim(
            kind=TASK_KIND,
            trade_date=trade_date,
            strategy=strategy,
            force=force,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )

        if not claim.claimed:
            conflict = claim.conflict
            # 抢占失败必然带回冲突行;没有则是 Store 契约被破坏,直接暴露
            if conflict is None:
                raise WorkbenchError(
                    "task_claim_inconsistent",
                    "抢占任务失败但未返回冲突任务,存储层状态异常",
                    status_code=500,
                )
            if conflict["status"] == "succeeded":
                # 已完成不是错误,返回既有任务详情供调用方展示。
                # 用 tracker.get() 重读整行而不是直接用 conflict:冲突字典不含
                # trade_date/kind/strategy,且完成时可能回写过真实 as_of,
                # 只有库里的行才是权威值。
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
                "scan_in_progress",
                f"{trade_date} 的 {strategy} 扫描正在运行",
                status_code=409,
                details=conflict,
            )

        # 抢占成功,提交后台执行
        self._executor.submit(
            self._run, claim.task_id, strategy, online, record, trade_date
        )
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND,
            "trade_date": trade_date,
            "strategy": strategy,
            "created_at": self.tracker.now(),
            "reused": False,
        }

    def _scan_visible(
        self, *, strategy: str, online: bool, record: bool, visible_as_of: str
    ) -> ScanResult:
        """按可见日截面执行一次扫描。

        在线摄取语义照旧:先向 Tushare 确认最新交易日,把日历和最新截面写进库
        (原始行情/K 线/总览仍看最新数据,不受可见闸门限制),再用刷新后的基准日
        重算可见日。只有持续摄取最新交易日,窗口才会往前推。

        评分输入固定 as_of=可见日:显式把日期传进 prepare_scan_data,不再让它
        自己去确认最新交易日,否则隐藏窗口里的行情又会被拿去打分。
        """
        settings = load_settings()
        client = None
        target = visible_as_of
        if online:
            client = _make_client(settings)
            base, _confirmed_rows = confirm_latest_trade_date(
                client, int(settings["data"]["min_daily_rows"])
            )
            start_cal = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
            with Store(self.db_path, ensure_schema=True) as store:
                ingest_calendar(
                    store, client, start_cal, _calendar_lookahead_end(), exchange=EXCHANGE
                )
                ingest_snapshot(store, client, base)
                window = resolve_window(
                    store, settings, exchange=EXCHANGE, base_session=base
                )
            # 在线基准日以 Tushare 确认为准,可见日随之重算;不可用直接上抛,
            # 由 _run() 落成 failed 并带上原因。
            target = require_visible_as_of(window)

        prepared = prepare_scan_data(
            strategy_name=strategy,
            online=online,
            db_path=str(self.db_path),
            settings_override=settings,
            client=client,
            as_of=target,
        )
        validate_scan_integrity(prepared)
        return score_prepared_scan(prepared, record=record)

    def _run(
        self,
        task_id: str,
        strategy: str,
        online: bool,
        record: bool,
        visible_as_of: str,
    ) -> None:
        """后台线程执行扫描,更新 task_runs 状态。

        失败时先落库再原样上抛:任务表留下 failed 与错误详情供 API 查询,
        同时日志里保留完整堆栈。绝不静默吞掉异常。
        """
        self.tracker.mark_running(task_id)

        try:
            result = self._scan_visible(
                strategy=strategy,
                online=online,
                record=record,
                visible_as_of=visible_as_of,
            )
        except Exception as error:
            self.tracker.finish(
                task_id,
                status="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            logger.exception("扫描任务 %s(%s)失败", task_id, strategy)
            raise

        self.tracker.finish(
            task_id,
            status="succeeded",
            result={
                "run_id": result.run_id,
                "as_of": result.as_of,
                "strategy": result.strategy,
                "candidate_count": result.candidate_count,
                "scored_count": result.scored_count,
                "passed_count": result.passed_count,
                "final_count": len(result.final),
            },
            # 回写真实 as_of:在线抓到更新交易日时校正幂等键
            trade_date=result.as_of,
        )

    def get(self, job_id: str) -> dict:
        """查询任务状态。job_id 即 task_id。"""
        task = self.tracker.get(job_id)
        if task is None:
            raise WorkbenchError(
                "scan_job_not_found",
                "扫描任务不存在",
                status_code=404,
            )
        return task

    def current_job(self) -> dict | None:
        """返回最近一次活跃或完成的扫描任务,供 overview 页展示。

        旧实现只返回内存中的活跃任务;新实现查询 task_runs 最新记录。
        """
        return self.tracker.latest(kind=TASK_KIND)

    def recent(self, *, limit: int = 20) -> list[dict]:
        """最近的扫描任务列表,按时间倒序。供状态查询与运行历史使用。"""
        if limit <= 0:
            raise WorkbenchError("invalid_limit", "limit 必须为正整数", status_code=400)
        return self.tracker.recent(kind=TASK_KIND, limit=limit)

    def shutdown(self) -> None:
        """关闭线程池。服务停止时调用。"""
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["ScanManager", "TASK_KIND", "STALE_AFTER_SECONDS"]
