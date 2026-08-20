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
from engine.run_scan import scan_completion_payload
from engine.visibility import (
    LookaheadBlocked,
    VisibilityWindow,
    backfill_sessions,
    ensure_visible,
    resolve_window,
)

from app.errors import WorkbenchError, safe_error_message
from app.services.one_click import OneClickRunner, STEP_NAMES
from app.services.one_click import OneClickRunner as ContractRunner
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "one_click_pipeline"

# 补齐最近若干可见交易日的协调器任务。它自己不跑链条,只按日期串行驱动
# TASK_KIND 的单日任务,便于前端用一个 job_id 观察整批补齐进度。
TASK_KIND_BACKFILL = "one_click_backfill"

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
        self._pi_agent_client: object | None = None

    def set_pi_agent_client(self, client: object | None) -> None:
        """Set the lifespan-owned Pi client used by subsequently started pipelines."""
        self._pi_agent_client = client

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

    def workflow_definition(self) -> dict:
        """Return the fixed workflow contract without starting a task."""
        config = self.config()
        latest = self.tracker.latest(kind=TASK_KIND)
        latest_result = (latest or {}).get("result") or {}
        return {
            "steps": ContractRunner.step_contract(),
            "strategy": config.strategy,
            "online": config.online,
            "data_cutoff_at": latest_result.get("data_cutoff_at"),
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
        """提交一次一键全流程。

        参数:
            trade_date: 手动指定目标交易日(YYYYMMDD 或带横线)。给了就必须
                是日历里的开市日,否则 400——猜一个日期会让整批数据挂错。
            strategy / online: 覆盖配置;None 表示沿用 settings.schedule。
            force: 绕过"已成功"检查,强制重跑同一批次。
            ignore_gate: 保留旧接口字段。手动一键任务会先更新日历，因此提交前
                不再使用旧日历做阻塞判断。

        幂等:同一 (交易日, 策略) 已 succeeded -> 返回既有详情 + reused=True;
        有活跃任务 -> 409;僵死任务 -> 自动抢占重试。
        """
        config = self.config()
        target_strategy = strategy or config.strategy
        target_online = config.online if online is None else bool(online)
        target_date, gate_decision = self._resolve_trade_date(
            trade_date=trade_date,
            config=config,
            online=target_online,
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

        # 未手动指定日期时，target_date 仅用于提交阶段的幂等抢占；真实 as_of
        # 必须由 calendar 步骤在更新日历后确认，不能把“今天”冒充已完整交易日。
        runner_trade_date = target_date if trade_date is not None else None
        try:
            self._executor.submit(
                self._run,
                claim.task_id,
                runner_trade_date,
                target_strategy,
                target_online,
            )
        except Exception as error:
            public_message = safe_error_message(error)
            self.tracker.finish(
                claim.task_id,
                status="failed",
                result={"run_id": claim.task_id, "current_step": "preflight", "steps": []},
                error={
                    "type": type(error).__name__,
                    "message": public_message,
                    "completed_steps": [],
                    "failed_step": "preflight",
                },
            )
            raise
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

    def _visibility_window(self, config: ScheduleConfig) -> VisibilityWindow:
        """本地只读可见窗口。提交阶段不联网,只按已入库日历与行情判定。"""
        settings = load_settings()
        with Store(self.db_path, ensure_schema=False) as store:
            return resolve_window(store, settings, exchange=config.exchange)

    def _resolve_trade_date(
        self,
        *,
        trade_date: Optional[str],
        config: ScheduleConfig,
        online: bool,
        ignore_gate: bool,
        now: Optional[datetime],
    ) -> tuple[str, Optional[GateDecision]]:
        """生成任务抢占日期;可见日期闸门在提交阶段就拦住前视请求。

        手动指定的日期必须 <= 可见日(基准日往前退 visibility_delay_sessions
        个开市日),否则 400 拒绝。静默改写成可见日是不行的:调用方会以为自己
        拿到了请求的那一天。交易日真伪仍由更新日历后的一键流程确认。
        """
        if trade_date is not None:
            try:
                target = normalize_trade_date(trade_date)
            except ScheduleConfigError as exc:
                raise WorkbenchError(
                    "invalid_trade_date", str(exc), status_code=400
                ) from exc
            try:
                visible = ensure_visible(target, self._visibility_window(config))
            except LookaheadBlocked as exc:
                raise WorkbenchError(exc.code, str(exc), status_code=400) from exc
            return visible, None

        # 未指定日期:在线与离线都用可见日当抢占键。后台 calendar 步骤确认的
        # 基准日只会更新,可见日才是"允许写入的最新交易日",用它当幂等键才能
        # 和后台真实 as_of 对上。
        window = self._visibility_window(config)
        if window.visible_as_of is not None:
            return window.visible_as_of, None

        # 空库首次运行还没有可作为幂等键的业务日期,只能临时占用墙钟当天;
        # calendar/market_data 完成后会把任务日期回写为真实 as_of。
        moment = now or datetime.now()
        return moment.strftime("%Y%m%d"), None

    # ------------------------------------------------------------ 执行
    def _run(
        self,
        task_id: str,
        trade_date: Optional[str],
        strategy: str,
        online: bool,
        *,
        refresh_latest: bool = True,
        collect_live_news: bool = True,
    ) -> None:
        """后台线程执行一键流程，并持久化每个步骤快照。"""
        progress: dict = {
            "run_id": task_id,
            "current_step": None,
            "steps": [],
            "group_counts": {},
            "as_of": None,
            "data_cutoff_at": None,
        }
        completed_atomically = False

        def on_step(snapshot: dict) -> None:
            progress.update(snapshot)
            self.tracker.progress(task_id, dict(progress))

        def on_complete(result: dict, experiment: tuple[dict, object, object]) -> None:
            nonlocal completed_atomically
            run_row, decisions, scan_result = experiment
            now = self.tracker.now()
            with Store(self.db_path, ensure_schema=True) as store:
                store.record_experiment(
                    run_row,
                    decisions,
                    task_completion={
                        "task_id": task_id,
                        "now": now,
                        "trade_date": result["as_of"],
                        "result_json": TaskTracker._dump(result),
                    },
                    scan_completion=scan_completion_payload(scan_result),
                )
            completed_atomically = True
        try:
            self.tracker.mark_running(task_id)
            runner = (
                OneClickRunner(self.db_path, pi_client=self._pi_agent_client)
                if self._pi_agent_client is not None
                else OneClickRunner(self.db_path)
            )
            result = runner.run(
                run_id=task_id,
                strategy=strategy,
                trade_date=trade_date,
                online=online,
                exchange=self.config().exchange,
                refresh_latest=refresh_latest,
                collect_live_news=collect_live_news,
                on_step=on_step,
                on_complete=on_complete,
            )
            if completed_atomically:
                return

            # API 测试可替换不写实验的执行器；真实执行器只要创建了实验，任务与
            # 实验就必须由 on_complete 原子提交，不能在这里补写一个假成功。
            with Store(self.db_path, ensure_schema=False) as store:
                experiment = store.experiment_run(task_id)
            if experiment is not None and experiment["status"] != "failed":
                raise RuntimeError("四组实验已创建但任务未原子完成")
            self.tracker.finish(
                task_id,
                status="succeeded",
                result=result,
                trade_date=result["as_of"],
            )

        except Exception as error:
            if completed_atomically:
                logger.error(
                    "盘后任务链 %s 已原子成功，忽略提交后的异常类型 %s",
                    task_id,
                    type(error).__name__,
                )
                return
            public_message = safe_error_message(error)
            failed_step = self._next_step_name(progress["steps"])
            progress["current_step"] = failed_step
            self.tracker.finish(
                task_id,
                status="failed",
                result=progress,
                error={
                    "type": type(error).__name__,
                    "message": public_message,
                    "completed_steps": list(progress["steps"]),
                    "failed_step": failed_step,
                },
            )
            logger.error(
                "盘后任务链 %s(%s/%s)失败: %s",
                task_id,
                trade_date,
                strategy,
                public_message,
            )
            raise

    @staticmethod
    def _next_step_name(steps: list[dict]) -> Optional[str]:
        """推断失败发生在哪一步:已完成步骤之后的第一个步骤。"""
        done = {step["name"] for step in steps}
        for name in STEP_NAMES:
            if name not in done:
                return name
        return None

    # ------------------------------------------------- 补齐最近若干可见交易日
    def backfill(
        self,
        *,
        count: int = 20,
        strategy: Optional[str] = None,
        online: Optional[bool] = None,
        force: bool = False,
    ) -> dict:
        """提交一次"补齐最近 count 个可见交易日"的协调器任务。

        日期由可见窗口给出:以可见日结尾、由旧到新的最多 count 个开市日。
        隐藏窗口内的日期一天都不会出现在列表里,所以补齐本身不引入前视偏差。

        幂等键是 (TASK_KIND_BACKFILL, 列表最后一天, 策略),与单日链条分开:
        协调器自己不写行情,单日结果仍归属 TASK_KIND 的那条任务。
        """
        if not 1 <= count <= 120:
            raise WorkbenchError(
                "invalid_backfill_count",
                f"补齐天数必须在 1 到 120 之间,实际为 {count}",
                status_code=400,
            )
        config = self.config()
        target_strategy = strategy or config.strategy
        target_online = config.online if online is None else bool(online)

        window = self._visibility_window(config)
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                dates = backfill_sessions(
                    store, exchange=config.exchange, window=window, count=count
                )
            except LookaheadBlocked as exc:
                raise WorkbenchError(exc.code, str(exc), status_code=400) from exc
        if not dates:
            raise WorkbenchError(
                "no_backfill_sessions",
                "可见窗口内没有可补齐的交易日",
                status_code=400,
            )

        target_date = dates[-1]
        claim = self.tracker.claim(
            kind=TASK_KIND_BACKFILL,
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
                    "抢占补齐任务失败但未返回冲突任务,存储层状态异常",
                    status_code=500,
                )
            if conflict["status"] == "succeeded":
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
                "backfill_in_progress",
                f"截至 {target_date} 的 {target_strategy} 补齐任务正在运行",
                status_code=409,
                details=conflict,
            )

        try:
            self._executor.submit(
                self._run_backfill,
                claim.task_id,
                dates,
                target_strategy,
                target_online,
                force,
            )
        except Exception as error:
            public_message = safe_error_message(error)
            self.tracker.finish(
                claim.task_id,
                status="failed",
                result=self._initial_backfill_progress(dates),
                error={
                    "type": type(error).__name__,
                    "code": "backfill_submit_failed",
                    "message": public_message,
                    "failed_date": None,
                },
            )
            raise
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND_BACKFILL,
            "trade_date": target_date,
            "strategy": target_strategy,
            "online": target_online,
            "dates": list(dates),
            "count": len(dates),
            "created_at": self.tracker.now(),
            "reused": False,
        }

    @staticmethod
    def _initial_backfill_progress(dates: list[str]) -> dict:
        """补齐进度的初始形态。字段固定,前端可以直接按 key 渲染。"""
        return {
            "dates": list(dates),
            "current_date": None,
            "completed": [],
            "reused": [],
            "failed_date": None,
            "failed": [],
            "remaining": list(dates),
        }

    def _run_backfill(
        self,
        task_id: str,
        dates: list[str],
        strategy: str,
        online: bool,
        force: bool,
    ) -> None:
        """后台单线程内由旧到新串行补齐，单日失败只记录警告。

        这里直接同步调用 `_run`,不再往同一个单工作线程的执行器里 submit——
        那会让协调器等待一个永远排在自己后面的任务,直接自锁。
        """
        progress = self._initial_backfill_progress(dates)
        try:
            self.tracker.mark_running(task_id)
            for index, date in enumerate(dates):
                progress["current_date"] = date
                self.tracker.progress(task_id, dict(progress))
                claim = self.tracker.claim(
                    kind=TASK_KIND,
                    trade_date=date,
                    strategy=strategy,
                    force=force,
                    stale_after_seconds=STALE_AFTER_SECONDS,
                )
                if not claim.claimed:
                    conflict = claim.conflict
                    if conflict is None:
                        raise WorkbenchError(
                            "task_claim_inconsistent",
                            "抢占单日任务失败但未返回冲突任务,存储层状态异常",
                            status_code=500,
                        )
                    if conflict["status"] != "succeeded":
                        message = f"{date} 的 {strategy} 盘后任务链状态为 {conflict['status']}，本日跳过"
                        progress["failed_date"] = progress["failed_date"] or date
                        progress["failed"].append(
                            {
                                "date": date,
                                "code": "pipeline_in_progress",
                                "message": message,
                            }
                        )
                    else:
                        # 已经跑成功过的日期直接复用,不重跑
                        progress["reused"].append(date)
                else:
                    try:
                        # refresh_latest 只在第一天为 True:整批补齐共用同一份
                        # "最新交易日"快照,每天都重新摄取纯属浪费 Tushare 额度。
                        # collect_live_news 一律 False:补历史不许把今天的热榜
                        # 算进历史批次。
                        self._run(
                            claim.task_id,
                            date,
                            strategy,
                            online,
                            refresh_latest=index == 0,
                            collect_live_news=False,
                        )
                    except Exception as error:
                        progress["failed_date"] = progress["failed_date"] or date
                        progress["failed"].append(
                            {
                                "date": date,
                                "code": getattr(error, "code", None) or "daily_pipeline_failed",
                                "message": safe_error_message(error),
                            }
                        )
                    else:
                        progress["completed"].append(date)
                progress["remaining"] = list(dates[index + 1 :])
                self.tracker.progress(task_id, dict(progress))
            progress["current_date"] = None
            progress["has_warnings"] = bool(progress["failed"])
            self.tracker.finish(task_id, status="succeeded", result=progress)
        except Exception as error:
            public_message = safe_error_message(error)
            self.tracker.finish(
                task_id,
                status="failed",
                result=progress,
                error={
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None) or "backfill_failed",
                    "message": public_message,
                    "failed_date": progress["failed_date"],
                    "completed": list(progress["completed"]),
                    "reused": list(progress["reused"]),
                },
            )
            logger.error(
                "补齐任务 %s(%s~%s/%s)在 %s 失败: %s",
                task_id,
                dates[0],
                dates[-1],
                strategy,
                progress["failed_date"],
                public_message,
            )
            raise

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

    def recent(self, *, limit: int = 20, kind: Optional[str] = None) -> list[dict]:
        """最近任务。kind=None 沿用一键链条;传 TASK_KIND_BACKFILL 查补齐协调器。"""
        if limit <= 0:
            raise WorkbenchError("invalid_limit", "limit 必须为正整数", status_code=400)
        return self.tracker.recent(kind=kind or TASK_KIND, limit=limit)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "PipelineManager",
    "TASK_KIND",
    "TASK_KIND_BACKFILL",
    "STALE_AFTER_SECONDS",
]
