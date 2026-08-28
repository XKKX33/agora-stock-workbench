"""task_runs 表的统一访问层。

扫描任务与盘后任务链都需要同一套东西:抢占业务幂等键、心跳、落库完成状态、
按 kind 查历史。把它收在一处,避免两个 Manager 各自复制一份 JSON 解析和
字段装饰逻辑——那种复制最终一定会漂移(一边补了字段另一边没补)。

这一层不认识 HTTP。查不到任务返回 None,由上层决定是 404 还是别的语义。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.db import Store
from engine.security import redact_for_client

# 心跳超过这个秒数未更新,视为进程崩溃遗留的僵死任务,允许被抢占
DEFAULT_STALE_AFTER_SECONDS = 3600


@dataclass(frozen=True)
class ClaimResult:
    """抢占结果。

    claimed=True  -> task_id 可用,调用方负责执行。
    claimed=False -> conflict 必然非空,里面是拦住本次抢占的那一行。
    """

    claimed: bool
    task_id: str
    conflict: Optional[dict]


class TaskTracker:
    """task_runs 的读写封装。按 (kind, trade_date, strategy) 做业务幂等。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 写
    def claim(
        self,
        *,
        kind: str,
        trade_date: str,
        strategy: str,
        force: bool = False,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> ClaimResult:
        """抢占一个业务幂等键。返回是否抢到,以及拦住它的冲突行。"""
        task_id = uuid.uuid4().hex
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                claimed, conflict = store.claim_task(
                    task_id=task_id,
                    kind=kind,
                    trade_date=trade_date,
                    strategy=strategy,
                    now=self.now(),
                    stale_after_seconds=stale_after_seconds,
                    force=force,
                )
        return ClaimResult(claimed=bool(claimed), task_id=task_id, conflict=conflict)

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                store.mark_task_running(task_id, self.now())

    def heartbeat(self, task_id: str) -> None:
        """刷新心跳。长链任务每步之间调用,否则会被误判僵死并被抢占。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                store.task_heartbeat(task_id, self.now())
    def update_progress(
        self,
        task_id: str,
        *,
        stage: str,
        step: int,
        total: int,
        message: str,
        detail: str = "",
        level: str = "info",
    ) -> None:
        """追加真实阶段、日志和明细，页面刷新后仍可恢复。"""
        if not stage or step < 1 or total < 1 or step > total:
            raise ValueError("任务进度阶段或步数无效")
        if level not in {"info", "warning", "error"}:
            raise ValueError("任务日志级别无效")
        now = self.now()
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                task = store.get_task(task_id)
                if task is None:
                    raise KeyError(f"任务不存在: {task_id}")
                payload = self.parse_json(task.get("result_json")) or {}
                steps = list(payload.get("steps") or [])
                for item in steps:
                    if item.get("status") == "running":
                        item["status"] = "succeeded"
                current = next((item for item in steps if item.get("name") == stage), None)
                if current is None:
                    current = {"name": stage}
                    steps.append(current)
                current.update({"status": "running", "detail": detail or message, "step": step, "total": total})
                logs = list((payload.get("progress") or {}).get("logs") or [])
                logs.append({"at": now, "level": level, "message": message, "detail": detail or message})
                payload["steps"] = steps
                payload["progress"] = {
                    "stage": stage,
                    "step": step,
                    "total": total,
                    "percent": int(step * 100 / total),
                    "message": message,
                    "logs": logs[-100:],
                }
                store.update_task_progress(task_id, now, self._dump(payload) or "{}")

    def progress(self, task_id: str, result: dict) -> None:
        """持久化当前步骤和全部已完成步骤，供刷新后恢复。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_task_progress(
                    task_id, self.now(), self._dump(result) or "{}"
                )

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        result: Optional[dict] = None,
        error: Optional[dict] = None,
        trade_date: Optional[str] = None,
    ) -> None:
        """落库终态，并保留运行期间的阶段和日志。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                existing = store.get_task(task_id)
                if existing is None:
                    raise KeyError(f"任务不存在: {task_id}")
                previous = self.parse_json(existing.get("result_json")) or {}
                merged = dict(previous)
                merged.update(result or {})
                previous_progress = dict(previous.get("progress") or {})
                final_progress = dict((result or {}).get("progress") or {})
                if previous_progress:
                    if final_progress.get("logs"):
                        previous_progress["logs"] = final_progress["logs"]
                    final_progress.pop("logs", None)
                    previous_progress.update(final_progress)
                    merged["progress"] = previous_progress
                previous_steps = list(previous.get("steps") or [])
                if previous_steps:
                    terminal_step_status = "failed" if status == "failed" else "succeeded"
                    merged["steps"] = [
                        {**item, "status": terminal_step_status if item.get("status") == "running" else item.get("status")}
                        for item in previous_steps
                    ]
                store.finish_task(
                    task_id=task_id,
                    now=self.now(),
                    status=status,
                    result_json=self._dump(merged),
                    error_json=self._dump(error),
                    trade_date=trade_date,
                )

    # ------------------------------------------------------------ 读
    def get(self, task_id: str) -> Optional[dict]:
        """按 task_id 取任务行(已装饰)。不存在返回 None。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=False) as store:
                task = store.get_task(task_id)
        return self.decorate(task) if task is not None else None

    def recent(self, *, kind: Optional[str] = None, limit: int = 20) -> list[dict]:
        """最近任务,按创建时间倒序。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=False) as store:
                frame = store.recent_tasks(limit=limit, kind=kind)
        if frame.empty:
            return []
        return [self.decorate(row) for row in frame.to_dict(orient="records")]

    def latest(self, *, kind: Optional[str] = None) -> Optional[dict]:
        items = self.recent(kind=kind, limit=1)
        return items[0] if items else None

    # ------------------------------------------------------------ 工具
    @classmethod
    def decorate(cls, task: dict) -> dict:
        """统一对外形态:补 job_id 别名,展开并脱敏 result/error。

        这是任务行流向浏览器的唯一出口。两件事必须在这里做完:
        1. 错误脱敏——不然 C:\\Users\\<用户名>\\... 这类磁盘布局会渲染到页面上;
        2. 丢掉 result_json / error_json 原始列——它们是库内部表示,
           留着等于把刚脱敏掉的原文又原样带出去一份。
        """
        out = dict(task)
        raw_result = out.pop("result_json", None)
        raw_error = out.pop("error_json", None)
        out["job_id"] = out.get("task_id")
        out["result"] = cls.parse_json(raw_result)
        out["error"] = cls._redact_error(cls.parse_json(raw_error))
        return out

    @staticmethod
    def _redact_error(error: Optional[dict]) -> Optional[dict]:
        """错误里的字符串值逐个脱敏,结构保持不变。"""
        if not error:
            return error
        return {
            key: redact_for_client(value) if isinstance(value, str) else value
            for key, value in error.items()
        }

    @staticmethod
    def parse_json(value: object) -> Optional[dict]:
        """解析任务行里的 JSON 列。空值返回 None;非法 JSON 直接上抛。

        不吞 JSONDecodeError:库里存着坏 JSON 是真实故障,静默返回 None
        会让调用方以为"任务没有结果",掩盖写入端的问题。
        """
        if not isinstance(value, str) or not value:
            return None
        return json.loads(value)

    @staticmethod
    def _dump(payload: Optional[dict]) -> Optional[str]:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()
