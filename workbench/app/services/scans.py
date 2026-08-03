from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from engine.config import load_settings
from engine.db import Store
from engine.run_scan import run_scan

from app.errors import WorkbenchError

logger = logging.getLogger(__name__)


class ScanManager:
    """扫描任务管理器,持久化到 task_runs 表,支持跨进程幂等与崩溃恢复。

    改动要点:
    - 不再用内存 dict 保存任务状态,全部写入 task_runs 表。
    - 调用 Store.claim_task() 抢占业务幂等键 (kind="scan", trade_date=as_of, strategy)。
    - 已有 succeeded 记录时不重复扫描;已有僵死任务时自动抢占重试。
    - 服务重启后状态仍在库中,可继续查询历史任务。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
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

        task_id = uuid.uuid4().hex
        now = self._now()

        with Store(self.db_path, ensure_schema=True) as store:
            claimed, conflict = store.claim_task(
                task_id=task_id,
                kind="scan",
                trade_date=trade_date,
                strategy=strategy,
                now=now,
                stale_after_seconds=3600,
                force=force,
            )

        if not claimed:
            # 抢占失败必然带回冲突行;没有则是 Store 契约被破坏,直接暴露
            if conflict is None:
                raise WorkbenchError(
                    "task_claim_inconsistent",
                    "抢占任务失败但未返回冲突任务,存储层状态异常",
                    status_code=500,
                )
            if conflict["status"] == "succeeded":
                # 已完成不是错误,返回既有任务详情供调用方展示。
                # 用 get() 重读整行而不是直接用 conflict:claim_task 返回的
                # 冲突字典不含 trade_date/kind/strategy,且完成时可能回写过
                # 真实 as_of,只有库里的行才是权威值。
                out = self.get(conflict["task_id"])
                out["reused"] = True
                return out
            raise WorkbenchError(
                "scan_in_progress",
                f"{trade_date} 的 {strategy} 扫描正在运行",
                status_code=409,
                details=conflict,
            )

        # 抢占成功,提交后台执行
        self._executor.submit(self._run, task_id, strategy, online, record)
        return {
            "job_id": task_id,
            "task_id": task_id,
            "status": "queued",
            "kind": "scan",
            "trade_date": trade_date,
            "strategy": strategy,
            "created_at": now,
            "reused": False,
        }

    def _run(self, task_id: str, strategy: str, online: bool, record: bool) -> None:
        """后台线程执行扫描,更新 task_runs 状态。

        失败时先落库再原样上抛:任务表留下 failed 与错误详情供 API 查询,
        同时日志里保留完整堆栈。绝不静默吞掉异常。
        """
        now = self._now()
        with Store(self.db_path, ensure_schema=True) as store:
            store.mark_task_running(task_id, now)

        try:
            result = run_scan(
                strategy_name=strategy,
                online=online,
                db_path=str(self.db_path),
                record=record,
            )
            payload = {
                "run_id": result.run_id,
                "as_of": result.as_of,
                "strategy": result.strategy,
                "candidate_count": result.candidate_count,
                "scored_count": result.scored_count,
                "passed_count": result.passed_count,
                "final_count": len(result.final),
            }
            with Store(self.db_path, ensure_schema=True) as store:
                store.finish_task(
                    task_id=task_id,
                    now=self._now(),
                    status="succeeded",
                    result_json=json.dumps(payload, ensure_ascii=False),
                    error_json=None,
                    # 回写真实 as_of:在线抓到更新交易日时校正幂等键
                    trade_date=result.as_of,
                )
        except Exception as error:
            err_payload = {
                "type": type(error).__name__,
                "message": str(error),
            }
            with Store(self.db_path, ensure_schema=True) as store:
                store.finish_task(
                    task_id=task_id,
                    now=self._now(),
                    status="failed",
                    result_json=None,
                    error_json=json.dumps(err_payload, ensure_ascii=False),
                )
            logger.exception("扫描任务 %s(%s)失败", task_id, strategy)
            raise

    def get(self, job_id: str) -> dict:
        """查询任务状态。job_id 即 task_id。"""
        with Store(self.db_path, ensure_schema=False) as store:
            task = store.get_task(job_id)
        if task is None:
            raise WorkbenchError(
                "scan_job_not_found",
                "扫描任务不存在",
                status_code=404,
            )
        return self._decorate(task)

    def current_job(self) -> dict | None:
        """返回最近一次活跃或完成的扫描任务,供 overview 页展示。

        旧实现只返回内存中的活跃任务;新实现查询 task_runs 最新记录。
        """
        items = self.recent(limit=1)
        return items[0] if items else None

    def recent(self, *, limit: int = 20) -> list[dict]:
        """最近的扫描任务列表,按时间倒序。供状态查询与运行历史使用。"""
        if limit <= 0:
            raise WorkbenchError("invalid_limit", "limit 必须为正整数", status_code=400)
        with Store(self.db_path, ensure_schema=False) as store:
            frame = store.recent_tasks(limit=limit, kind="scan")
        if frame.empty:
            return []
        return [self._decorate(row) for row in frame.to_dict(orient="records")]

    def shutdown(self) -> None:
        """关闭线程池。服务停止时调用。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _decorate(cls, task: dict) -> dict:
        """统一任务行的对外形态:补 job_id 别名,展开 result/error JSON。"""
        out = dict(task)
        out["job_id"] = out["task_id"]
        out["result"] = cls._parse_json(task.get("result_json"))
        out["error"] = cls._parse_json(task.get("error_json"))
        return out

    @staticmethod
    def _parse_json(value: object) -> dict | None:
        """解析任务行里的 JSON 列。空值返回 None;非法 JSON 直接上抛。

        不吞 JSONDecodeError:库里存着坏 JSON 是真实故障,静默返回 None
        会让调用方以为"任务没有结果",掩盖写入端的问题。
        """
        if not isinstance(value, str) or not value:
            return None
        return json.loads(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
