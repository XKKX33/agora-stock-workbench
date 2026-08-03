"""盘后任务链的调度线程。

它只做一件事:每隔 `tick_seconds` 问一次闸门"现在该不该跑",该跑就交给
`PipelineManager.start()`,然后把这一轮的结论记下来供状态接口展示。

几个刻意的设计选择:

1. **调度线程不判重、不管幂等**。幂等完全靠 `task_runs` 的业务键
   (kind, trade_date, strategy)。原因:进程重启、多进程部署、手动触发都可能
   在同一交易日抢同一批次,只有落库的键能跨进程生效,线程内的记忆不行。
   所以"已经跑过了"表现为 `start()` 返回 `reused=True`,或抛 409 冲突,
   两者都是正常结果,不是错误。
2. **线程不会因为一次触发失败而退出**。失败会写进 `last_error` 并打完整堆栈,
   下一轮继续。理由:任务链自身的失败已经落在 task_runs 里明确暴露了,
   再把调度线程一起弄死,只会让后面所有交易日都静默不跑——那才是真正的隐藏故障。
3. **`enabled=False` 时不启动线程,但状态照常上报**。状态里 `enabled` 与
   `running` 是两个独立字段:`enabled=false & running=false` 是正常关闭,
   `enabled=true & running=false` 是故障(线程没起来或已崩溃)。
   两者在页面上必须能区分,不能混成一个"没动静"。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from engine.schedule import ScheduleConfigError

from app.errors import WorkbenchError
from app.services.pipelines import PipelineManager
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

# 触发时被这些错误码拦住属于正常调度结果,不记为故障。
# pipeline_in_progress:上一轮还在跑(链条比 tick 长时必然出现)。
# pipeline_not_due:闸门在读配置与真正触发之间跨过了边界。
BENIGN_CODES = frozenset({"pipeline_in_progress", "pipeline_not_due"})


class CloseScheduler:
    """后台轮询调度器。start()/stop() 由 FastAPI 生命周期钩子调用。"""

    def __init__(self, manager: PipelineManager) -> None:
        self.manager = manager
        self._thread: Optional[threading.Thread] = None
        # Event 而不是 sleep 轮询:stop() 要能立刻打断等待,
        # 否则 tick_seconds 设成 600 时服务关闭要卡十分钟。
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_tick_at: Optional[str] = None
        self._last_tick_detail: Optional[str] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """启动轮询线程。返回是否真的启动了。

        两种不启动的情况都不是静默失败:配置非法会把原因写进 `last_error`,
        `schedule.enabled=false` 会写进 `last_tick_detail`,状态接口都能看到。
        """
        try:
            config = self.manager.config()
        except WorkbenchError as error:
            # 配置错误不静默:线程不启动,原因写进 last_error 供状态接口展示。
            self._record(error=f"调度配置非法,调度线程未启动: {error.message}")
            logger.error("调度配置非法,调度线程未启动: %s", error.message)
            return False

        if not config.enabled:
            self._record(
                detail="schedule.enabled=false,未启动调度线程(可手动触发盘后任务链)",
                error=None,
            )
            logger.info("schedule.enabled=false,未启动调度线程")
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="quant-scheduler",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "调度线程已启动: run_after=%s tick=%ss strategy=%s online=%s",
            config.run_after_text,
            config.tick_seconds,
            config.strategy,
            config.online,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 轮询
    def _loop(self) -> None:
        while not self._stop.is_set():
            # 每轮重读配置:改了 settings.yaml 里的运行时间不必重启服务。
            # tick_seconds 也随之生效,所以间隔从本轮读到的配置取。
            try:
                tick = self.manager.config().tick_seconds
            except (WorkbenchError, ScheduleConfigError) as error:
                self._record(error=f"读取调度配置失败: {error}")
                logger.exception("读取调度配置失败")
                tick = 60
            else:
                self.tick()
            self._stop.wait(timeout=tick)

    def tick(self, *, now: Optional[datetime] = None) -> dict:
        """执行一轮调度判断。返回本轮结论,供测试与手动诊断直接调用。

        这个方法自己吞掉异常并写进 last_error,是为了让轮询线程活下去
        (见模块文档第 2 点)。测试要断言失败行为时看返回值里的 action/error,
        不要指望它抛。
        """
        try:
            config = self.manager.config()
        except (WorkbenchError, ScheduleConfigError) as error:
            detail = f"读取调度配置失败: {error}"
            self._record(detail=detail, error=detail)
            logger.exception("读取调度配置失败")
            return {"action": "error", "detail": detail}

        if not config.enabled:
            detail = "schedule.enabled=false,调度器只上报状态不触发"
            self._record(detail=detail, error=None)
            return {"action": "disabled", "detail": detail}

        try:
            decision = self.manager.gate(now=now)
        except (WorkbenchError, ScheduleConfigError) as error:
            detail = f"闸门判定失败: {error}"
            self._record(detail=detail, error=detail)
            logger.exception("闸门判定失败")
            return {"action": "error", "detail": detail}

        if not decision.should_run:
            self._record(detail=decision.detail, error=None)
            return {
                "action": "skip",
                "reason": decision.reason,
                "detail": decision.detail,
            }

        # 到这里闸门放行。是否真的新建任务由 task_runs 的幂等键决定:
        # 已跑成功过就拿到 reused=True,不重复写入同一批次。
        try:
            job = self.manager.start(now=now)
        except WorkbenchError as error:
            if error.code in BENIGN_CODES:
                detail = f"跳过本轮({error.code}): {error.message}"
                self._record(detail=detail, error=None)
                return {"action": "skip", "reason": error.code, "detail": detail}
            detail = f"触发盘后任务链失败({error.code}): {error.message}"
            self._record(detail=detail, error=detail)
            logger.error("触发盘后任务链失败: %s", detail)
            return {"action": "error", "detail": detail}
        except Exception as error:  # noqa: BLE001 - 线程必须活下去,原因记进状态
            detail = f"触发盘后任务链异常: {type(error).__name__}: {error}"
            self._record(detail=detail, error=detail)
            logger.exception("触发盘后任务链异常")
            return {"action": "error", "detail": detail}

        if job.get("reused"):
            detail = f"{job.get('trade_date')} 的批次已完成,未重复触发"
            self._record(detail=detail, error=None)
            return {"action": "reused", "detail": detail, "job": job}

        detail = f"已触发 {job.get('trade_date')} 的盘后任务链 {job.get('job_id')}"
        self._record(detail=detail, error=None)
        logger.info("%s", detail)
        return {"action": "started", "detail": detail, "job": job}

    # ------------------------------------------------------------ 状态
    def _record(self, *, detail: Optional[str] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self._last_tick_at = TaskTracker.now()
            if detail is not None:
                self._last_tick_detail = detail
            # error=None 是"本轮正常",要清掉上一次的故障;不清会让页面
            # 永远显示一个早已恢复的错误。
            self._last_error = error

    def status(self, *, now: Optional[datetime] = None) -> dict:
        """调度状态 = PipelineManager 的配置/闸门/最近任务 + 线程自身运行情况。"""
        payload = self.manager.status(now=now)
        # 先取 running(它自己会拿锁),再进临界区读快照字段。
        # 不要在持锁期间调用 self.running:threading.Lock 不可重入,会死锁。
        running = self.running
        with self._lock:
            payload.update(
                running=running,
                last_tick_at=self._last_tick_at,
                last_tick_detail=self._last_tick_detail,
                last_error=self._last_error,
            )
        return payload


__all__ = ["CloseScheduler", "BENIGN_CODES"]
