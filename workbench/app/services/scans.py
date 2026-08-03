from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.config import load_settings
from engine.db import Store
from engine.run_scan import run_scan

from app.errors import WorkbenchError
from app.services.tasks import DEFAULT_STALE_AFTER_SECONDS, TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "scan"

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

        注意:抢占时只能用本地 latest_confirmed_date 预解析 trade_date。
        在线模式下 Tushare 可能返回更新的交易日,run_scan 实际写入的 as_of
        会晚于此处的键。_run() 完成时会把真实 as_of 回写到 task_runs.trade_date,
        使幂等键与真实批次对齐,后续同 as_of 的重跑仍能被拦住。
        """
        settings = load_settings()
        min_rows = int(settings["data"]["min_daily_rows"])

        with Store(self.db_path, ensure_schema=False) as store:
            trade_date = store.latest_confirmed_date(min_rows) or store.latest_date()
        if trade_date is None:
            raise WorkbenchError(
                "no_market_data",
                "数据库无可用交易日数据,无法启动扫描",
                status_code=503,
            )

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
        self._executor.submit(self._run, claim.task_id, strategy, online, record)
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

    def _run(self, task_id: str, strategy: str, online: bool, record: bool) -> None:
        """后台线程执行扫描,更新 task_runs 状态。

        失败时先落库再原样上抛:任务表留下 failed 与错误详情供 API 查询,
        同时日志里保留完整堆栈。绝不静默吞掉异常。
        """
        self.tracker.mark_running(task_id)

        try:
            result = run_scan(
                strategy_name=strategy,
                online=online,
                db_path=str(self.db_path),
                record=record,
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
