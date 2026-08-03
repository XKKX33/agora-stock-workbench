"""盘后任务链的应用层管理器。

职责边界:

- `engine.close_pipeline` 负责跑链条,它不认识 HTTP、也不认识 task_runs。
- `engine.schedule` 负责回答"此刻该不该为哪个交易日跑"(纯函数)。
- 本模块把两者接起来:解析目标交易日 -> 抢占幂等键 -> 后台执行 -> 落库终态,
  并把存储层的结果翻译成 HTTP 语义。

三条与 ScanManager 保持一致的约定:

1. **"已完成"不是错误**。同一 (交易日, 策略) 已经跑成功过,返回既有任务详情
   并带 `reused=True`,由路由层决定用 200 还是 202,不走错误信封。
2. **抢占失败必须带回冲突行**。没带回来说明存储层契约被破坏,直接 500 暴露,
   不去猜"大概是并发吧"。
3. **失败先落库再原样上抛**。task_runs 里留下 failed 与错误详情供页面展示,
   日志里保留完整堆栈,绝不静默吞异常。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from engine.close_pipeline import PipelineResult, StepResult, run_close_pipeline
from engine.config import load_settings
from engine.db import Store
from engine.schedule import (
    GateDecision,
    ScheduleConfig,
    ScheduleConfigError,
    decide_due_run,
    is_trading_day,
    load_schedule_config,
    normalize_trade_date,
)

from app.errors import WorkbenchError
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "close_pipeline"

# 链条比单次扫描长(摄取 + 扫描 + 回填 + 舆情 + 复盘),僵死判定阈值相应放宽。
# 每步之间都会刷心跳,所以正常运行的长任务不会被误判;这个值只用来兜住
# 进程被杀掉、连心跳都停了的情况。
STALE_AFTER_SECONDS = 7200


class PipelineManager:
    """盘后任务链管理器。单工作线程,保证同进程内不并发跑链条。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.tracker = TaskTracker(self.db_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="quant-pipeline"
        )

    # ------------------------------------------------------------ 配置与闸门
    def config(self) -> ScheduleConfig:
        """读取当前调度配置。配置非法时直接 400 暴露,不回退默认值。"""
        try:
            return load_schedule_config(load_settings())
        except ScheduleConfigError as exc:
            raise WorkbenchError(
                "schedule_config_invalid", str(exc), status_code=400
            ) from exc

    def gate(self, *, now: Optional[datetime] = None) -> GateDecision:
        """当前时刻的闸门结论。now 可注入,便于测试与状态接口复用。"""
        config = self.config()
        moment = now or datetime.now()
        with Store(self.db_path, ensure_schema=False) as store:
            open_dates = store.open_dates(
                config.exchange, moment.strftime("%Y%m%d"), 1
            )
            calendar_max = store.calendar_max(config.exchange)
        return decide_due_run(
            now=moment,
            run_after=config.run_after,
            latest_open_date=open_dates[-1] if open_dates else None,
            calendar_max=calendar_max,
        )

    def status(self, *, now: Optional[datetime] = None) -> dict:
        """调度状态:配置 + 闸门结论 + 最近一次链条任务。供总览页展示。"""
        config = self.config()
        decision = self.gate(now=now)
        return {
            "enabled": config.enabled,
            "run_after": config.run_after_text,
            "exchange": config.exchange,
            "strategy": config.strategy,
            "online": config.online,
            "tick_seconds": config.tick_seconds,
            "gate": decision.as_dict(),
            "latest": self.tracker.latest(kind=TASK_KIND),
        }

    # ------------------------------------------------------------ 启动
    def start(
        self,
        *,
        trade_date: Optional[str] = None,
        strategy: Optional[str] = None,
        online: Optional[bool] = None,
        force: bool = False,
        ignore_gate: bool = False,
        now: Optional[datetime] = None,
    ) -> dict:
        """提交一次盘后任务链。

        参数:
            trade_date: 手动指定目标交易日(YYYYMMDD 或带横线)。给了就必须
                是日历里的开市日,否则 400——猜一个日期会让整批数据挂错。
            strategy / online: 覆盖配置;None 表示沿用 settings.schedule。
            force: 绕过"已成功"检查,强制重跑同一批次。
            ignore_gate: 手动触发时跳过**运行时间**闸门。注意它只跳过时间,
                不跳过交易日判定:日历缺失或过期时依旧拒绝运行。

        幂等:同一 (交易日, 策略) 已 succeeded -> 返回既有详情 + reused=True;
        有活跃任务 -> 409;僵死任务 -> 自动抢占重试。
        """
        config = self.config()
        target_strategy = strategy or config.strategy
        target_online = config.online if online is None else bool(online)
        target_date, gate_decision = self._resolve_trade_date(
            trade_date=trade_date,
            config=config,
            ignore_gate=ignore_gate,
            now=now,
        )

        claim = self.tracker.claim(
            kind=TASK_KIND,
            trade_date=target_date,
            strategy=target_strategy,
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
                # 已完成不是错误。用 tracker.get() 重读整行而不是直接用 conflict:
                # 冲突字典不含 trade_date/kind/strategy,且完成时可能回写过真实
                # as_of,只有库里的行才是权威值。
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
                "pipeline_in_progress",
                f"{target_date} 的 {target_strategy} 盘后任务链正在运行",
                status_code=409,
                details=conflict,
            )

        self._executor.submit(
            self._run, claim.task_id, target_date, target_strategy, target_online
        )
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND,
            "trade_date": target_date,
            "strategy": target_strategy,
            "online": target_online,
            "created_at": self.tracker.now(),
            "gate": gate_decision.as_dict() if gate_decision else None,
            "reused": False,
        }

    def _resolve_trade_date(
        self,
        *,
        trade_date: Optional[str],
        config: ScheduleConfig,
        ignore_gate: bool,
        now: Optional[datetime],
    ) -> tuple[str, Optional[GateDecision]]:
        """确定目标交易日,返回 (交易日, 闸门结论或 None)。

        手动指定日期时不跑闸门(用户明确要求补跑历史批次是合理需求),
        但仍校验它确实是开市日——这条不能让步,否则批次会挂在非交易日上。
        """
        if trade_date is not None:
            try:
                target = normalize_trade_date(trade_date)
            except ScheduleConfigError as exc:
                raise WorkbenchError(
                    "invalid_trade_date", str(exc), status_code=400
                ) from exc
            with Store(self.db_path, ensure_schema=False) as store:
                open_dates = store.open_dates(config.exchange, target, 1)
            if not open_dates:
                raise WorkbenchError(
                    "calendar_missing",
                    f"trade_cal 中没有 {config.exchange} 在 {target} 及之前的开市日记录,"
                    "无法确认它是交易日;请先回补交易日历",
                    status_code=503,
                )
            if not is_trading_day(target, open_dates):
                raise WorkbenchError(
                    "not_trading_day",
                    f"{target} 不是 {config.exchange} 的开市日,拒绝为它生成盘后批次",
                    status_code=400,
                )
            return target, None

        decision = self.gate(now=now)
        if decision.should_run:
            return decision.trade_date, decision

        # 闸门拒绝。ignore_gate 只能跳过"还没到运行时间",不能跳过日历问题:
        # 日历缺失或过期时连"目标是哪天"都不知道,硬跑只会把数据挂到错的键上。
        if ignore_gate and decision.trade_date:
            return decision.trade_date, decision
        if decision.trade_date is None:
            raise WorkbenchError(
                "calendar_unusable",
                decision.detail,
                status_code=503,
                details=decision.as_dict(),
            )
        raise WorkbenchError(
            "pipeline_not_due",
            decision.detail,
            status_code=409,
            details=decision.as_dict(),
        )

    # ------------------------------------------------------------ 执行
    def _run(self, task_id: str, trade_date: str, strategy: str, online: bool) -> None:
        """后台线程执行链条。每步刷心跳并把步骤结果写进任务结果。"""
        self.tracker.mark_running(task_id)
        steps: list[StepResult] = []

        def on_step(step: StepResult) -> None:
            # 心跳失败不捕获:写不进库说明存储有问题,继续跑只会掩盖故障。
            steps.append(step)
            self.tracker.heartbeat(task_id)

        try:
            result = run_close_pipeline(
                db_path=str(self.db_path),
                strategy=strategy,
                trade_date=trade_date,
                online=online,
                exchange=self.config().exchange,
                on_step=on_step,
            )
        except Exception as error:
            # 失败时把"已经跑完的步骤"一起落库:页面要能看出卡在哪一步、
            # 之前哪几步已经写过库了,只留一句错误信息是不够的。
            self.tracker.finish(
                task_id,
                status="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                    "completed_steps": [step.as_dict() for step in steps],
                    "failed_step": self._next_step_name(steps),
                },
            )
            logger.exception("盘后任务链 %s(%s/%s)失败", task_id, trade_date, strategy)
            raise

        self.tracker.finish(
            task_id,
            status="succeeded",
            result=result.as_dict(),
            # 回写真实 as_of:在线模式抓到更新交易日时校正幂等键,
            # 否则下一次同 as_of 的重跑会因为键不匹配而放行,幂等失效。
            trade_date=result.trade_date,
        )

    @staticmethod
    def _next_step_name(steps: list[StepResult]) -> Optional[str]:
        """推断失败发生在哪一步:已完成步骤之后的第一个步骤。"""
        from engine.close_pipeline import PIPELINE_STEPS

        done = {step.name for step in steps}
        for name in PIPELINE_STEPS:
            if name not in done:
                return name
        return None

    # ------------------------------------------------------------ 查询
    def get(self, job_id: str) -> dict:
        task = self.tracker.get(job_id)
        if task is None:
            raise WorkbenchError(
                "pipeline_job_not_found", "盘后任务链不存在", status_code=404
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


__all__ = ["PipelineManager", "PipelineResult", "TASK_KIND", "STALE_AFTER_SECONDS"]
