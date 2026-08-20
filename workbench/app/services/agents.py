"""多 agent 短线研判的应用层管理器。

分工:
- engine/agents.py      —— 纯编排:prompt、解析、并行调用、加权汇总(不认识数据库)
- 本模块                —— 数据装配:把库里的行情/技术指标/资金流/舆情装成快照,
                           用 TaskTracker 管理后台任务,结果落 agent_runs/agent_judgments

纪律(与舆情采集一致):
- 已完成不是错误:同一 as_of + 同一组参数已 succeeded,复用并带 reused=True;
- 抢占失败必须带回冲突行,没有就是存储层契约被破坏,直接 500;
- 失败先落库再原样上抛,绝不在后台线程里吞异常;
- AI 未配置/不可用时直接 503,不编造结果,不做规则模板降级。
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pandas as pd

from engine.agents import (
    AgentConfig,
    AgentOutputError,
    load_agent_config,
    run_judge,
    run_public_debate,
    run_single,
    status as agent_status,
)
from engine.ai import (
    AIConfig,
    AIUnavailableError,
    OpenAICompatibleClient,
    build_client,
    load_ai_config,
)
from engine.config import load_settings_with_local
from engine.db import Store
from engine.methodology import build_agent_brief
from engine.visibility import (
    LookaheadBlocked,
    VisibilityWindow,
    ensure_visible,
    require_visible_as_of,
    resolve_window,
)

from app.errors import WorkbenchError, safe_error_message
from app.repositories.market import MarketRepository
from app.services.agents_data import AgentDataMixin
from app.schemas.pi_agent import PiAgentRequest, PiLimits, PiMethodology, PiModelConfig
from app.services.pi_agent import PiAgentClient, PiAgentProtocolError
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)



class _AgentEventSubscription:
    """One listener registration owned by a single stream consumer."""

    _CLOSE = object()

    def __init__(self, bus: "AgentEventBus", run_id: str, after_seq: int) -> None:
        self._bus = bus
        self.run_id = str(run_id)
        self._after_seq = after_seq
        self._queue: queue.Queue = queue.Queue()
        self.owner_thread = threading.get_ident()
        self._closed = False
        self._replay_done = False
        self._replay: Optional[Iterator[dict]] = None

    def __iter__(self):
        return self

    def __next__(self) -> Optional[dict]:
        if self._closed:
            raise StopIteration
        if not self._replay_done:
            if self._replay is None:
                with Store(self._bus.db_path, ensure_schema=False) as store:
                    self._replay = iter(store.agent_events(self.run_id, after_seq=self._after_seq))
            try:
                event = next(self._replay)
            except StopIteration:
                self._replay_done = True
            else:
                self._after_seq = max(self._after_seq, int(event["seq"]))
                return event
        while not self._closed:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                return None
            if event is self._CLOSE:
                raise StopIteration
            if int(event["seq"]) > self._after_seq:
                self._after_seq = int(event["seq"])
                return event
        raise StopIteration

    def close(self) -> None:
        self._bus._close_subscription(self)


class AgentEventBus:
    """In-process wake-up layer; Store persistence remains source of truth."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._subscriptions: dict[str, list[_AgentEventSubscription]] = {}

    def publish(self, event: dict) -> dict:
        event = dict(event)
        event.setdefault("event_id", uuid.uuid4().hex)
        event.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        with Store(self.db_path, ensure_schema=True) as store:
            saved = store.append_agent_event(event)
        with self._lock:
            listeners = list(self._subscriptions.get(str(saved["run_id"]), []))
        for listener in listeners:
            listener._queue.put(saved)
        return saved

    def subscribe(self, run_id: str, after_seq: int = 0) -> _AgentEventSubscription:
        subscription = _AgentEventSubscription(self, run_id, after_seq)
        with self._lock:
            self._subscriptions.setdefault(subscription.run_id, []).append(subscription)
        return subscription

    def _close_subscription(self, subscription: _AgentEventSubscription) -> None:
        with self._lock:
            listeners = self._subscriptions.get(subscription.run_id, [])
            if subscription not in listeners:
                return
            listeners.remove(subscription)
            if not listeners:
                self._subscriptions.pop(subscription.run_id, None)
            subscription._closed = True
        subscription._queue.put(_AgentEventSubscription._CLOSE)

    def close(self, subscription_or_run_id: _AgentEventSubscription | str) -> None:
        """Close one subscription; run IDs remain supported for route cleanup."""
        if isinstance(subscription_or_run_id, _AgentEventSubscription):
            subscription_or_run_id.close()
            return
        run_id = str(subscription_or_run_id)
        owner = threading.get_ident()
        with self._lock:
            listeners = self._subscriptions.get(run_id, [])
            subscription = next(
                (item for item in reversed(listeners) if item.owner_thread == owner),
                None,
            )
        if subscription is not None:
            subscription.close()
TASK_KIND = "agent_judge"
STALE_AFTER_SECONDS = 7200  # 研判是长任务,心跳停 2 小时才算僵死

# 交易日历口径:全项目统一按上交所日历推可见窗口。
EXCHANGE = "SSE"


class AgentJudgeManager(AgentDataMixin):
    """多 agent 研判管理器。单工作线程,同进程内不并发跑两个研判。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.repository = MarketRepository(db_path)
        self.tracker = TaskTracker(db_path)
        self.event_bus = AgentEventBus(self.db_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quant-agent")
        self._pi_agent_status: dict[str, Any] = {"availability": "unavailable", "reason": "Pi Agent 尚未启动"}
        self._pi_agent_client: PiAgentClient | None = None

    def set_pi_agent_status(self, status: dict[str, Any], client: PiAgentClient | None = None) -> None:
        self._pi_agent_status = dict(status)
        if client is not None:
            self._pi_agent_client = client

    def events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> dict:
        if after_seq < 0 or limit < 1 or limit > 500:
            raise WorkbenchError("invalid_event_query", "事件序列或数量无效", status_code=400)
        with Store(self.db_path, ensure_schema=False) as store:
            if store.get_agent_run(run_id) is None:
                raise WorkbenchError(
                    "agent_judge_job_not_found", "研判任务不存在", status_code=404
                )
            items = store.agent_events(run_id, after_seq=after_seq, limit=limit)
            last_seq = store.agent_event_last_seq(run_id)
        next_seq = items[-1]["seq"] if items else after_seq
        return {
            "run_id": run_id,
            "items": items,
            "next_seq": next_seq,
            "has_more": next_seq < last_seq,
        }

    def _publish_event(self, run_id: str, event_type: str, *, stage: str = "", role: str = "", content: Any = None, status: str = "", ts_code: str | None = None, round_no: Any = None, citations: Any = None) -> dict:
        return self.event_bus.publish({"run_id": run_id, "event_type": event_type, "stage": stage, "role": role, "content": content if content is not None else {}, "citations": citations if citations is not None else [], "status": status, "ts_code": ts_code, "round_no": round_no})

    def _relay_pi_event(self, run_id: str, event: dict) -> dict:
        """把 Pi 的公开事件搬进本地事件表。role/stage/round_no 必须落到独立列，
        否则前端六格辩论面板按 role 取不到任何消息。"""
        round_no = event.get("round_no")
        return self._publish_event(
            run_id,
            str(event.get("event_type", event.get("type", "pi.event"))),
            stage=str(event.get("stage") or ""),
            role=str(event.get("role") or ""),
            content=event.get("data", event),
            status="running",
            ts_code=str(event["ts_code"]) if event.get("ts_code") else None,
            round_no=int(round_no) if isinstance(round_no, (int, float)) else None,
            citations=event.get("citations") if isinstance(event.get("citations"), list) else None,
        )

    def _configs(self) -> tuple[AgentConfig, AIConfig]:
        try:
            settings = load_settings_with_local()
            return load_agent_config(settings), load_ai_config(settings)
        except Exception as exc:
            raise WorkbenchError("agent_config_invalid", safe_error_message(exc), status_code=400) from exc

    def status(self) -> dict:
        agent, ai = self._configs()
        info = agent_status(agent, ai)
        info["pi_agent"] = dict(self._pi_agent_status)
        return info

    # ------------------------------------------------------------ 候选池
    def candidates(self, *, limit: int = 50, ts_codes: Optional[list[str]] = None) -> dict:
        """研判输入池预览。默认取最近一次扫描的候选;给了 ts_codes 则只列这些。"""
        if limit <= 0 or limit > 200:
            raise WorkbenchError(
                "invalid_limit", "limit 需在 1~200 之间", status_code=400
            )
        run, frame = self.repository.latest_scan_rows()
        if ts_codes:
            wanted = {c.strip().upper() for c in ts_codes if c and c.strip()}
            frame = frame[frame["ts_code"].isin(wanted)]
        items = []
        for _, row in frame.head(limit).iterrows():
            items.append(
                {
                    "ts_code": row["ts_code"],
                    "name": row["name"],
                    "industry": row["industry"],
                    "rank": int(row["rank"]) if pd.notna(row["rank"]) else None,
                    "total": round(float(row["total"]), 4) if pd.notna(row["total"]) else None,
                    "passed": bool(row["passed"]) if pd.notna(row["passed"]) else None,
                    "money_class": row["money_class"] if pd.notna(row["money_class"]) else None,
                }
            )
        return {"run_id": run["run_id"], "as_of": run["as_of"], "items": items}

    # ------------------------------------------------------------ 启动
    def start(
        self,
        *,
        candidates: Optional[int] = None,
        depth: Optional[int] = None,
        final_count: Optional[int] = None,
        ts_codes: Optional[list[str]] = None,
        force: bool = False,
        as_of: Optional[str] = None,
    ) -> dict:
        """发起一次多 agent 研判。

        as_of 省略时走默认口径(最近一次扫描的 as_of,没有则退到可见日);显式
        指定时先过可见闸门——这是输入错误,和 AI 配置无关,所以放在配置检查之前。
        """
        requested_as_of = (
            self._ensure_visible_as_of(as_of) if as_of is not None else None
        )
        agent, ai = self._configs()
        info = agent_status(agent, ai)
        if not agent.enabled:
            raise WorkbenchError(
                "agent_disabled",
                "多 agent 研判未启用(settings.yaml 的 agent.enabled=false)",
                status_code=503,
                details=info,
            )
        if info.get("availability") != "available":
            raise WorkbenchError(
                "agent_unconfigured",
                info.get("reason") or "AI 配置不完整",
                status_code=503,
                details=info,
            )

        candidates_n, depth_n, final_n = agent.clamp(
            candidates if candidates is not None else agent.default_candidates,
            depth if depth is not None else agent.default_depth,
            final_count if final_count is not None else agent.default_final,
        )

        as_of = requested_as_of or self._resolve_as_of()
        strategy = f"c{candidates_n}d{depth_n}f{final_n}"
        claim = self.tracker.claim(
            kind=TASK_KIND,
            trade_date=as_of,
            strategy=strategy,
            force=force,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )
        if not claim.claimed:
            return self._handle_conflict(claim, candidates_n, depth_n, final_n)

        created = self.tracker.now()
        with Store(self.db_path, ensure_schema=True) as store:
            store.record_agent_run(
                {
                    "run_id": claim.task_id,
                    "as_of": as_of,
                    "status": "queued",
                    "stage": "queued",
                    "candidates": candidates_n,
                    "depth": depth_n,
                    "final_count": final_n,
                    "progress_json": json.dumps(
                        {"stage": "queued", "step": 0, "total": 0, "message": "等待开始"},
                        ensure_ascii=False,
                    ),
                    "created_at": created,
                    "started_at": None,
                    "finished_at": None,
                    "heartbeat_at": created,
                    "error_json": None,
                    "result_json": None,
                }
            )
        self._executor.submit(
            self._run, claim.task_id, as_of, candidates_n, depth_n, final_n, ts_codes
        )
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND,
            "trade_date": as_of,
            "params": {
                "candidates": candidates_n,
                "depth": depth_n,
                "final": final_n,
            },
            "created_at": created,
            "reused": False,
        }

    # ------------------------------------------------------------ 单只研判
    def start_single(
        self,
        *,
        ts_code: str,
        force: bool = False,
        as_of: Optional[str] = None,
    ) -> dict:
        """对单只股票发起深度研判:三位分析师 + 多空辩论 + 风控。

        不走候选池/粗筛,直接用该股票的快照。任务复用现有 agent_runs 表,
        strategy 前缀用 single:<ts_code> 区分,方便结果查询区分单股/选股流程。

        as_of 显式指定时先过可见闸门:单票研判是最容易手工指定日期的入口,
        隐藏窗口里的快照绝不能拿来研判。
        """
        requested_as_of = (
            self._ensure_visible_as_of(as_of) if as_of is not None else None
        )
        agent, ai = self._configs()
        info = agent_status(agent, ai)
        if not agent.enabled:
            raise WorkbenchError(
                "agent_disabled",
                "多 agent 研判未启用(settings.yaml 的 agent.enabled=false)",
                status_code=503,
                details=info,
            )
        if self._pi_agent_client is None or self._pi_agent_status.get("availability") != "available":
            pi_info = {"availability": "unavailable", **self._pi_agent_status}
            raise WorkbenchError(
                "pi_agent_unavailable",
                pi_info.get("reason") or "Pi Agent 不可用",
                status_code=503,
                details=pi_info,
            )
        if info.get("availability") != "available":
            raise WorkbenchError(
                "agent_unconfigured",
                info.get("reason") or "AI 配置不完整",
                status_code=503,
                details=info,
            )

        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            raise WorkbenchError("invalid_ts_code", "请指定一只股票代码", status_code=400)

        as_of = requested_as_of or self._resolve_as_of()
        # 单股的候选资格由冻结输入一次性校验，避免与后续请求使用不同批次。
        strategy = f"single:{ts_code}"
        claim = self.tracker.claim(
            kind=TASK_KIND,
            trade_date=as_of,
            strategy=strategy,
            force=force,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )
        if not claim.claimed:
            return self._handle_conflict(claim, 1, 1, 1)

        created = self.tracker.now()
        with Store(self.db_path, ensure_schema=True) as store:
            store.record_agent_run(
                {
                    "run_id": claim.task_id,
                    "as_of": as_of,
                    "status": "queued",
                    "stage": "queued",
                    "candidates": 1,
                    "depth": 1,
                    "final_count": 1,
                    "progress_json": json.dumps(
                        {"stage": "queued", "step": 0, "total": 0, "message": "等待开始"},
                        ensure_ascii=False,
                    ),
                    "created_at": created,
                    "started_at": None,
                    "finished_at": None,
                    "heartbeat_at": created,
                    "error_json": None,
                    "result_json": None,
                }
            )
        self._executor.submit(self._run_single, claim.task_id, as_of, ts_code)
        return {
            "job_id": claim.task_id,
            "task_id": claim.task_id,
            "status": "queued",
            "kind": TASK_KIND,
            "trade_date": as_of,
            "mode": "single",
            "ts_code": ts_code,
            "params": {"candidates": 1, "depth": 1, "final": 1},
            "created_at": created,
            "reused": False,
        }

    def _run_single(self, task_id: str, as_of: str, ts_code: str) -> None:
        """后台执行单只 Pi 研判；输入和结果均经过正式协议校验。"""
        self.tracker.mark_running(task_id)
        self._publish_event(task_id, "run.started", stage="run", status="running")
        try:
            client = self._pi_agent_client
            if client is None or self._pi_agent_status.get("availability") != "available":
                raise WorkbenchError(
                    "pi_agent_unavailable",
                    self._pi_agent_status.get("reason") or "Pi Agent 不可用",
                    status_code=503,
                )
            frozen = self.freeze_agent_input(1, [ts_code], as_of)
            agent, ai = self._configs()
            request = PiAgentRequest(
                protocol_version="1",
                workflow_version="1",
                mode="single",
                trade_date=as_of,
                candidate_hash=frozen.candidate_hash,
                input_hash=frozen.input_hash,
                limits=PiLimits(coarse=1, deep=1, final=1),
                candidates=frozen.candidates,
                snapshots=frozen.snapshots,
                model=PiModelConfig(
                    provider=agent.provider or ai.provider,
                    model=agent.model or ai.model,
                    reasoning_effort=agent.reasoning_effort,
                    max_tokens=agent.max_tokens,
                ),
                methodology=PiMethodology(**build_agent_brief()),
            )
            pi_run_id = client.start_judgment(request)
            for event in client.stream_events(pi_run_id):
                self._relay_pi_event(task_id, event)
            result = client.get_result(pi_run_id, request)
            result_dict = result.model_dump(mode="json")
            self._persist_pi(task_id, result_dict, frozen.candidates)
            summary = {
                "as_of": as_of,
                "mode": "single",
                "run_id": pi_run_id,
                "input_hash": frozen.input_hash,
                "final": result_dict["final"],
            }
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    stage="done",
                    status="succeeded",
                    finished_at=self.tracker.now(),
                    result_json=json.dumps(summary, ensure_ascii=False),
                )
            self._publish_event(task_id, "run.completed", stage="done", status="succeeded")
            self.tracker.finish(task_id, status="succeeded", result=summary)
        except Exception as error:
            detail = {"type": type(error).__name__, "message": safe_error_message(error)}
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    status="failed",
                    stage="failed",
                    finished_at=self.tracker.now(),
                    error_json=json.dumps(detail, ensure_ascii=False),
                )
            self._publish_event(task_id, "run.failed", stage="failed", content=detail, status="failed")
            self.tracker.finish(task_id, status="failed", error=detail)
            logger.exception("Pi 单只研判 %s(%s)失败", task_id, ts_code)


    def _handle_conflict(self, claim, candidates_n, depth_n, final_n) -> dict:
        conflict = claim.conflict
        if conflict is None:
            raise WorkbenchError(
                "task_claim_inconsistent",
                "抢占任务失败但未返回冲突任务,存储层状态异常",
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
            "agent_judge_in_progress",
            "相同参数(候选/深度/最终数)的研判正在运行,先等它完成或强制重跑",
            status_code=409,
            details=conflict,
        )

    def _visibility_window(self) -> VisibilityWindow:
        """按本地基准日算可见窗口。研判读的是本地历史截面,这里不联网确认基准日。"""
        self.repository.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return resolve_window(
                store, load_settings_with_local(), exchange=EXCHANGE
            )

    def _ensure_visible_as_of(self, requested: str) -> str:
        """校验显式指定的研判日期:必须 <= 可见日,绝不静默改写成可见日。

        手工传日期是最容易触发前视的入口(直接把隐藏窗口里的行情喂给 Agent),
        所以命中就报 lookahead_blocked(400);窗口本身算不出来报 409。
        """
        window = self._visibility_window()
        try:
            return ensure_visible(str(requested), window)
        except LookaheadBlocked as exc:
            raise WorkbenchError(
                exc.code,
                str(exc),
                status_code=400 if exc.code == "lookahead_blocked" else 409,
                details=window.as_dict(),
            ) from exc

    def _resolve_as_of(self, requested: Optional[str] = None) -> str:
        """研判截面日期。

        - 显式指定:过可见闸门,落在隐藏窗口内直接拒绝。
        - 默认:优先最近一次扫描的 as_of——候选池就是那一批冻结出来的,用它自己的
          时点研判不是前视;没有扫描记录时退到可见日,而不是行情最新交易日,
          否则隐藏窗口里的行情会被直接喂给 Agent。
        """
        if requested is not None:
            return self._ensure_visible_as_of(requested)
        try:
            run, _ = self.repository.latest_scan_rows()
            return str(run["as_of"])
        except WorkbenchError:
            window = self._visibility_window()
            try:
                return require_visible_as_of(window)
            except LookaheadBlocked as exc:
                # 保留 no_market_data 语义(库里没有可研判的数据),
                # 但把可见窗口算不出来的真实原因写进 message 与 details。
                raise WorkbenchError(
                    "no_market_data",
                    f"数据库还没有可研判的交易日,无法研判;请先更新数据并扫描({exc})",
                    status_code=503,
                    details=window.as_dict(),
                ) from exc
    def _run(
        self, task_id: str, as_of: str, candidates_n: int, depth_n: int,
        final_n: int, ts_codes: Optional[list[str]],
    ) -> None:
        self.tracker.mark_running(task_id)
        self._publish_event(task_id, "run.started", stage="run", status="running")
        try:
            client = self._pi_agent_client
            if client is None or self._pi_agent_status.get("availability") != "available":
                raise WorkbenchError("pi_agent_unavailable", self._pi_agent_status.get("reason") or "Pi Agent 不可用", status_code=503)
            frozen = self.freeze_agent_input(candidates_n, ts_codes, as_of)
            agent, ai = self._configs()
            request = PiAgentRequest(
                protocol_version="1", workflow_version="1", mode="batch", trade_date=as_of,
                candidate_hash=frozen.candidate_hash, input_hash=frozen.input_hash,
                limits=PiLimits(coarse=candidates_n, deep=depth_n, final=final_n),
                candidates=frozen.candidates, snapshots=frozen.snapshots,
                model=PiModelConfig(provider=agent.provider or ai.provider, model=agent.model or ai.model,
                                    reasoning_effort=agent.reasoning_effort, max_tokens=agent.max_tokens),
                methodology=PiMethodology(**build_agent_brief()),
            )
            pi_run_id = client.start_judgment(request)
            for event in client.stream_events(pi_run_id):
                self._relay_pi_event(task_id, event)
            result = client.get_result(pi_run_id, request)
            result_dict = result.model_dump(mode="json")
            self._persist_pi(task_id, result_dict, frozen.candidates)
            summary = {"as_of": as_of, "run_id": pi_run_id, "final": result_dict["final"], "input_hash": frozen.input_hash}
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(task_id, stage="done", status="succeeded", finished_at=self.tracker.now(), result_json=json.dumps(summary, ensure_ascii=False))
            self._publish_event(task_id, "run.completed", stage="done", status="succeeded")
            self.tracker.finish(task_id, status="succeeded", result=summary)
        except Exception as error:
            detail = {"type": type(error).__name__, "message": safe_error_message(error)}
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(task_id, status="failed", stage="failed", finished_at=self.tracker.now(), error_json=json.dumps(detail, ensure_ascii=False))
            self._publish_event(task_id, "run.failed", stage="failed", content=detail, status="failed")
            self.tracker.finish(task_id, status="failed", error=detail)
            logger.exception("Pi agent 研判 %s(%s)失败", task_id, as_of)
    def _persist_pi(self, task_id: str, result: dict, candidates: list[dict]) -> None:
        names = {str(item.get("ts_code")): item.get("name", "") for item in candidates}
        rows = []
        for item in result.get("final", []):
            thesis = item.get("reason") or item["bull_case"]
            rows.append({"run_id": task_id, "ts_code": item["ts_code"], "name": names.get(item["ts_code"], ""), "industry": "", "rank": item["rank"], "score": item["score"], "stance": item["decision"], "thesis": thesis, "risks": json.dumps([item["bear_case"], item["risk_control"]], ensure_ascii=False), "stage_json": json.dumps(item, ensure_ascii=False)})
        if rows:
            with Store(self.db_path, ensure_schema=True) as store:
                store.upsert_agent_judgments(pd.DataFrame(rows))

    def _update_progress(self, task_id: str, stage: str, step: int, total: int, message: str) -> None:
        progress = {
            "stage": stage,
            "step": step,
            "total": total,
            "message": message,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with Store(self.db_path, ensure_schema=True) as store:
            store.update_agent_run(
                task_id,
                stage=stage,
                progress_json=json.dumps(progress, ensure_ascii=False),
                heartbeat_at=self.tracker.now(),
            )

    def _persist(self, task_id: str, result: dict) -> None:
        """把最终结论落 agent_judgments(含辩论与三位分析师详情)。"""
        rows = []
        for item in result["final"]:
            rows.append(
                {
                    "run_id": task_id,
                    "ts_code": item["ts_code"],
                    "name": item["name"],
                    "industry": item["industry"],
                    "rank": item["rank"],
                    "score": item["score"],
                    "stance": item["stance"],
                    "thesis": item["thesis"],
                    "risks": json.dumps(item.get("risks", []), ensure_ascii=False),
                    "stage_json": json.dumps(
                        {
                            "verdict": item["verdict"],
                            "action": item.get("action"),
                            "debate": item.get("debate"),
                            "deep": item.get("deep"),
                            "public_debate": item.get("public_debate", []),
                            "event_seq": item.get("event_seq", []),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        if not rows:
            return
        with Store(self.db_path, ensure_schema=True) as store:
            store.upsert_agent_judgments(pd.DataFrame(rows))


    # ------------------------------------------------------------ 查询
    def get(self, job_id: str) -> dict:
        task = self.tracker.get(job_id)
        if task is None:
            raise WorkbenchError(
                "agent_judge_job_not_found", "研判任务不存在", status_code=404
            )
        with Store(self.db_path, ensure_schema=False) as store:
            run = store.get_agent_run(job_id)
            judgments = store.agent_judgments(job_id)
            event_count = store.agent_event_last_seq(job_id)
        task["progress"] = TaskTracker.parse_json(run.get("progress_json")) if run else None
        task["params"] = (
            {
                "candidates": run.get("candidates"),
                "depth": run.get("depth"),
                "final": run.get("final_count"),
            }
            if run
            else None
        )
        task["judgments"] = [
            {
                **{k: v for k, v in row.items() if k not in ("stage_json", "risks")},
                "risks": json.loads(row["risks"]) if row.get("risks") else [],
                "stage": json.loads(row["stage_json"]) if row.get("stage_json") else {},
            }
            for row in judgments.to_dict(orient="records")
        ]
        task["event_count"] = event_count
        task["report_available"] = bool(task["judgments"])
        return task

    def recent(self, *, limit: int = 20) -> list[dict]:
        if limit <= 0 or limit > 100:
            raise WorkbenchError(
                "invalid_limit", "limit 需在 1~100 之间", status_code=400
            )
        with Store(self.db_path, ensure_schema=False) as store:
            # Workflow agent stages persist their report under the pipeline task
            # id, so include only pipeline tasks that have a matching agent run.
            frame = store.con.execute(
                """
                SELECT task_runs.*
                FROM task_runs
                WHERE task_runs.kind = ?
                   OR (
                       task_runs.kind = ?
                       AND EXISTS (
                           SELECT 1
                           FROM agent_runs
                           WHERE agent_runs.run_id = task_runs.task_id
                       )
                   )
                ORDER BY task_runs.created_at DESC, task_runs.task_id DESC
                LIMIT ?
                """,
                [TASK_KIND, "one_click_pipeline", limit],
            ).df()
            items = [
                TaskTracker.decorate(row)
                for row in frame.to_dict(orient="records")
            ]
            for item in items:
                item["event_count"] = store.agent_event_last_seq(item["task_id"])
                item["report_available"] = bool(item.get("status") == "succeeded")
        return items

    def results(self, *, as_of: Optional[str] = None, limit: int = 20) -> dict:
        """已完成的研判结果列表,只含 succeeded 批次,可按交易日 as_of 过滤。

        这里不设可见闸门:读的是已经落库的研判结论,不碰任何行情,查一条历史
        记录不会产生前视。闸门放在生成侧(start / start_single / _resolve_as_of),
        堵住"用隐藏窗口里的数据做研判";查询侧再拦一遍只会让老批次读不出来。
        """
        if limit <= 0 or limit > 100:
            raise WorkbenchError(
                "invalid_limit", "limit 需在 1~100 之间", status_code=400
            )
        with Store(self.db_path, ensure_schema=False) as store:
            runs = store.recent_agent_runs(limit=limit, as_of=as_of, status="succeeded")
            items = []
            for row in runs.to_dict(orient="records"):
                judgments = store.agent_judgments(row["run_id"])
                event_count = store.agent_event_last_seq(row["run_id"])
                items.append(
                    {
                        "run_id": row["run_id"],
                        "as_of": row.get("as_of"),
                        "status": row.get("status"),
                        "created_at": row.get("created_at"),
                        "finished_at": row.get("finished_at"),
                        "params": {
                            "candidates": row.get("candidates"),
                            "depth": row.get("depth"),
                            "final": row.get("final_count"),
                        },
                        "summary": TaskTracker.parse_json(row.get("result_json")),

                        "judgments": [
                            {
                                **{k: v for k, v in j.items() if k not in ("stage_json", "risks")},
                                "risks": json.loads(j["risks"]) if j.get("risks") else [],
                                "stage": json.loads(j["stage_json"]) if j.get("stage_json") else {},
                            }
                            for j in judgments.to_dict(orient="records")
                        ],
                        "event_count": event_count,
                        "report_available": not judgments.empty,
                    }
                )
        return {"as_of": as_of, "items": items}

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["AgentJudgeManager", "TASK_KIND", "STALE_AFTER_SECONDS"]

