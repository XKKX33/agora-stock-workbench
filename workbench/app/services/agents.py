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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from engine.agents import (
    AgentConfig,
    AgentOutputError,
    load_agent_config,
    run_judge,
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

from app.errors import WorkbenchError
from app.repositories.market import MarketRepository
from app.services.agents_data import AgentDataMixin
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "agent_judge"
STALE_AFTER_SECONDS = 7200  # 研判是长任务,心跳停 2 小时才算僵死


class AgentJudgeManager(AgentDataMixin):
    """多 agent 研判管理器。单工作线程,同进程内不并发跑两个研判。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.repository = MarketRepository(db_path)
        self.tracker = TaskTracker(db_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="quant-agent"
        )

    # ------------------------------------------------------------ 配置
    def _configs(self) -> tuple[AgentConfig, AIConfig]:
        try:
            settings = load_settings_with_local()
            return load_agent_config(settings), load_ai_config(settings)
        except Exception as exc:
            raise WorkbenchError(
                "agent_config_invalid", str(exc), status_code=400
            ) from exc

    def status(self) -> dict:
        agent, ai = self._configs()
        return agent_status(agent, ai)

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
    ) -> dict:
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

        as_of = self._resolve_as_of()
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
    ) -> dict:
        """对单只股票发起深度研判:三位分析师 + 多空辩论 + 风控。

        不走候选池/粗筛,直接用该股票的快照。任务复用现有 agent_runs 表,
        strategy 前缀用 single:<ts_code> 区分,方便结果查询区分单股/选股流程。
        """
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

        ts_code = (ts_code or "").strip().upper()
        if not ts_code:
            raise WorkbenchError("invalid_ts_code", "请指定一只股票代码", status_code=400)

        as_of = self._resolve_as_of()
        # 单股也用候选池口径校验存在性
        run, frame = self.repository.latest_scan_rows()
        if ts_code not in set(frame["ts_code"]):
            raise WorkbenchError(
                "stock_not_in_pool",
                f"{ts_code} 不在最近候选池里,请确认代码或先扫描",
                status_code=404,
            )
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
        """后台执行单只研判。"""
        self.tracker.mark_running(task_id)
        agent, ai = self._configs()
        client: Optional[OpenAICompatibleClient] = None
        try:
            client = build_client(agent.ai_config(ai))
            snapshot = self._load_snapshot(ts_code, as_of)
            if not snapshot.get("stock"):
                raise WorkbenchError(
                    "stock_no_data", f"{ts_code} 没有可用行情快照", status_code=404
                )

            def on_progress(stage: str, step: int, total: int, message: str) -> None:
                self._update_progress(task_id, stage, step, total, message)

            result = run_single(
                client, agent, as_of=as_of, snapshot=snapshot, on_progress=on_progress
            )
            self._persist(task_id, result)
            summary = {
                "as_of": as_of,
                "mode": "single",
                "final": [
                    {
                        "ts_code": item["ts_code"],
                        "name": item["name"],
                        "score": item["score"],
                        "verdict": item["verdict"],
                    }
                    for item in result["final"]
                ],
            }
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    stage="done",
                    status="succeeded",
                    finished_at=self.tracker.now(),
                    result_json=json.dumps(summary, ensure_ascii=False),
                )
            self.tracker.finish(task_id, status="succeeded", result=summary)
        except Exception as error:  # noqa: BLE001 - 落库后原样上抛
            detail = {"type": type(error).__name__, "message": str(error)}
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    status="failed",
                    stage="failed",
                    finished_at=self.tracker.now(),
                    error_json=json.dumps(detail, ensure_ascii=False),
                )
            self.tracker.finish(task_id, status="failed", error=detail)
            logger.exception("单只研判 %s(%s)失败", task_id, ts_code)
            raise
        finally:
            if client is not None:
                client.close()


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

    def _resolve_as_of(self) -> str:
        """研判截面:优先最近一次扫描的 as_of(候选池就是它的),否则取行情最新日。"""
        try:
            run, _ = self.repository.latest_scan_rows()
            return str(run["as_of"])
        except WorkbenchError:
            as_of = self.repository.latest_trade_date()
            if not as_of:
                raise WorkbenchError(
                    "no_market_data",
                    "数据库还没有行情数据,无法研判;请先更新数据并扫描",
                    status_code=503,
                )
            return str(as_of)

    # ------------------------------------------------------------ 执行
    def _run(
        self,
        task_id: str,
        as_of: str,
        candidates_n: int,
        depth_n: int,
        final_n: int,
        ts_codes: Optional[list[str]],
    ) -> None:
        """后台线程执行研判。失败先落库再上抛。"""
        self.tracker.mark_running(task_id)
        agent, ai = self._configs()
        client: Optional[OpenAICompatibleClient] = None
        try:
            client = build_client(agent.ai_config(ai))
            pool = self._build_pool(candidates_n, ts_codes, as_of)
            if not pool:
                raise WorkbenchError(
                    "empty_candidate_pool",
                    "候选池为空:最近一次扫描没有候选股票,或指定的 ts_codes 都不在扫描结果里",
                    status_code=400,
                )

            def on_progress(stage: str, step: int, total: int, message: str) -> None:
                self._update_progress(task_id, stage, step, total, message)

            result = run_judge(
                client,
                agent,
                as_of=as_of,
                candidates=pool,
                loader=lambda code: self._load_snapshot(code, as_of),
                candidates_limit=candidates_n,
                depth=depth_n,
                final_count=final_n,
                on_progress=on_progress,
            )
            self._persist(task_id, result)
            summary = {
                "as_of": as_of,
                "final": [
                    {
                        "ts_code": item["ts_code"],
                        "name": item["name"],
                        "score": item["score"],
                        "verdict": item["verdict"],
                    }
                    for item in result["final"]
                ],
            }
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    stage="done",
                    status="succeeded",
                    finished_at=self.tracker.now(),
                    result_json=json.dumps(summary, ensure_ascii=False),
                )
            self.tracker.finish(task_id, status="succeeded", result=summary)
        except Exception as error:  # noqa: BLE001 - 落库后原样上抛
            detail = {"type": type(error).__name__, "message": str(error)}
            with Store(self.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    task_id,
                    status="failed",
                    stage="failed",
                    finished_at=self.tracker.now(),
                    error_json=json.dumps(detail, ensure_ascii=False),
                )
            self.tracker.finish(task_id, status="failed", error=detail)
            logger.exception("多 agent 研判 %s(%s)失败", task_id, as_of)
            raise
        finally:
            if client is not None:
                client.close()

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
        return task

    def recent(self, *, limit: int = 20) -> list[dict]:
        if limit <= 0 or limit > 100:
            raise WorkbenchError(
                "invalid_limit", "limit 需在 1~100 之间", status_code=400
            )
        return self.tracker.recent(kind=TASK_KIND, limit=limit)

    def results(self, *, as_of: Optional[str] = None, limit: int = 20) -> dict:
        """已完成的研判结果列表,只含 succeeded 批次,可按交易日 as_of 过滤。"""
        if limit <= 0 or limit > 100:
            raise WorkbenchError(
                "invalid_limit", "limit 需在 1~100 之间", status_code=400
            )
        with Store(self.db_path, ensure_schema=False) as store:
            runs = store.recent_agent_runs(
                limit=limit, as_of=as_of, status="succeeded"
            )
            items = []
            for row in runs.to_dict(orient="records"):
                judgments = store.agent_judgments(row["run_id"])
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
                    }
                )
        return {"as_of": as_of, "items": items}

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["AgentJudgeManager", "TASK_KIND", "STALE_AFTER_SECONDS"]

