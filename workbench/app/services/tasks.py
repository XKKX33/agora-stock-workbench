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
        """落库终态。trade_date 非空时回写,用于把幂等键对齐到真实批次。"""
        with self._lock:
            with Store(self.db_path, ensure_schema=True) as store:
                store.finish_task(
                    task_id=task_id,
                    now=self.now(),
                    status=status,
                    result_json=self._dump(result),
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
        """统一对外形态:补 job_id 别名,展开 result/error JSON。"""
        out = dict(task)
        out["job_id"] = out.get("task_id")
        out["result"] = cls.parse_json(task.get("result_json"))
        out["error"] = cls.parse_json(task.get("error_json"))
        return out

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
