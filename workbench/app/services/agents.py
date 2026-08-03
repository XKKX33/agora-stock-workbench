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

import numpy as np
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
from app.services.tasks import TaskTracker

logger = logging.getLogger(__name__)

TASK_KIND = "agent_judge"
STALE_AFTER_SECONDS = 7200  # 研判是长任务,心跳停 2 小时才算僵死


class AgentJudgeManager:
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

    # ------------------------------------------------------------ 数据装配
    def _build_pool(
        self, candidates_n: int, ts_codes: Optional[list[str]], as_of: str
    ) -> list[dict]:
        """构造粗筛输入:最近扫描候选(可按 ts_codes 收窄),每只带紧凑行情行。"""
        run, frame = self.repository.latest_scan_rows()
        data = frame
        if ts_codes:
            wanted = {c.strip().upper() for c in ts_codes if c and c.strip()}
            data = data[data["ts_code"].isin(wanted)]
        pool: list[dict] = []
        for _, row in data.head(candidates_n).iterrows():
            code = row["ts_code"]
            try:
                history = self.repository.history(code, as_of, 40)
            except Exception:
                history = pd.DataFrame()
            pool.append(self._compact_row(row, history))
        return pool

    @staticmethod
    def _compact_row(row: pd.Series, history: pd.DataFrame) -> dict:
        """粗筛用的紧凑行:收盘/涨跌/5日/20日/量比/MACD状态/资金确认。"""
        close = None
        pct = None
        pct_5d = None
        pct_20d = None
        macd_state = ""
        if not history.empty:
            closes = history["close"].astype(float)
            close = float(closes.iloc[-1])
            pct = float(history["pct_chg"].iloc[-1]) if pd.notna(history["pct_chg"].iloc[-1]) else None
            if len(closes) >= 6:
                pct_5d = round((closes.iloc[-1] / closes.iloc[-6] - 1) * 100, 2)
            if len(closes) >= 21:
                pct_20d = round((closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2)
            macd_state = AgentJudgeManager._macd_state(closes)
        return {
            "ts_code": row["ts_code"],
            "name": row["name"] if pd.notna(row["name"]) else "",
            "industry": row["industry"] if pd.notna(row["industry"]) else "",
            "close": close,
            "pct_chg": pct,
            "pct_5d": pct_5d,
            "pct_20d": pct_20d,
            "volume_ratio": None,
            "macd_state": macd_state,
            "money_class": row["money_class"] if pd.notna(row["money_class"]) else "",
        }

    @staticmethod
    def _macd_state(closes: pd.Series) -> str:
        """把日线 MACD 压成一句话状态,粗筛只喂状态不喂原始序列。"""
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        last_dif, last_dea = float(dif.iloc[-1]), float(dea.iloc[-1])
        prev_dif = float(dif.iloc[-2]) if len(dif) > 1 else last_dif
        cross = "金叉" if prev_dif <= last_dea and last_dif > last_dea else (
            "死叉" if prev_dif >= last_dea and last_dif < last_dea else ""
        )
        zone = "零轴上" if last_dif >= 0 else "零轴下"
        return f"{zone}{cross or ('红柱' if last_dif >= last_dea else '绿柱')}"

    def _load_snapshot(self, ts_code: str, as_of: str) -> dict:
        """深度学习/辩论用的完整快照:行情+指标+周线+资金流+舆情。"""
        history = self.repository.history(ts_code, as_of, 150)
        moneyflow = self.repository.moneyflow(ts_code, as_of, 10)
        with Store(self.db_path, ensure_schema=False) as store:
            row = store.con.execute(
                "SELECT ts_code, symbol, name, industry FROM stock_basic WHERE ts_code = ?",
                [ts_code],
            ).fetchone()
            info = (
                dict(zip(("ts_code", "symbol", "name", "industry"), row))
                if row
                else {"ts_code": ts_code, "name": "", "industry": ""}
            )
            stock_news = store.news_for_link(
                link_type="stock", link_key=ts_code, as_of=as_of, limit=15
            )
            industry = info.get("industry") or ""
            industry_news = (
                store.news_for_link(
                    link_type="industry", link_key=industry, as_of=as_of, limit=8
                )
                if industry
                else pd.DataFrame()
            )
        return {
            "stock": self._stock_brief(info, history),
            "daily": self._daily_brief(history),
            "weekly": self._weekly_brief(history),
            "moneyflow": self._moneyflow_brief(moneyflow),
            "news": {
                "source_note": "舆情输入双源互补:① TrendRadar 全网热榜已入库数据;② 质量评估字段(关联度/来源可信度/情绪/时效)借鉴 TradingAgents-CN 口径。未新增采集器。",
                "stock_items": self._news_brief(stock_news),
                "industry_items": self._news_brief(industry_news),
            },
        }

    @staticmethod
    def _stock_brief(info: dict, history: pd.DataFrame) -> dict:
        close = None
        if not history.empty:
            close = round(float(history["close"].iloc[-1]), 2)
        return {
            "ts_code": info.get("ts_code"),
            "name": info.get("name"),
            "industry": info.get("industry"),
            "close": close,
        }

    @staticmethod
    def _daily_brief(history: pd.DataFrame) -> dict:
        if history.empty:
            return {}
        closes = history["close"].astype(float)
        highs = history["high"].astype(float)
        lows = history["low"].astype(float)
        ma = {f"ma{n}": _round(float(closes.tail(n).mean()), 2) for n in (5, 10, 20, 60)}

        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        hist = (dif - dea) * 2

        low9 = lows.rolling(9).min()
        high9 = highs.rolling(9).max()
        rsv = (closes - low9) / (high9 - low9).replace(0, np.nan) * 100
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        j = 3 * k - 2 * d

        diff = closes.diff()
        up = diff.clip(lower=0)
        down = -diff.clip(upper=0)
        avg_up = up.ewm(alpha=1 / 6, adjust=False).mean()
        avg_down = down.ewm(alpha=1 / 6, adjust=False).mean()
        rsi6 = 100 - 100 / (1 + avg_up / avg_down.replace(0, np.nan))

        mid = closes.rolling(20).mean()
        std = closes.rolling(20).std()

        recent = history.tail(5)
        recent_5 = [
            {
                "date": str(r["trade_date"]),
                "pct_chg": _round(float(r["pct_chg"]), 2) if pd.notna(r["pct_chg"]) else None,
                "vol": _round(float(r["vol"]), 0) if pd.notna(r["vol"]) else None,
                "amount": _round(float(r["amount"]), 0) if pd.notna(r["amount"]) else None,
            }
            for _, r in recent.iterrows()
        ]
        return {
            "ma": ma,
            "macd": {
                "dif": _round(float(dif.iloc[-1]), 3),
                "dea": _round(float(dea.iloc[-1]), 3),
                "hist": _round(float(hist.iloc[-1]), 3),
            },
            "kdj": {
                "k": _round(float(k.iloc[-1]), 2),
                "d": _round(float(d.iloc[-1]), 2),
                "j": _round(float(j.iloc[-1]), 2),
            },
            "rsi6": _round(float(rsi6.iloc[-1]), 2) if pd.notna(rsi6.iloc[-1]) else None,
            "boll": {
                "upper": _round(float(mid.iloc[-1] + 2 * std.iloc[-1]), 2) if pd.notna(std.iloc[-1]) else None,
                "mid": _round(float(mid.iloc[-1]), 2),
                "lower": _round(float(mid.iloc[-1] - 2 * std.iloc[-1]), 2) if pd.notna(std.iloc[-1]) else None,
            },
            "recent_5": recent_5,
            "range_20": {
                "high": _round(float(highs.tail(20).max()), 2),
                "low": _round(float(lows.tail(20).min()), 2),
                "pct_20d": _round((closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2)
                if len(closes) >= 21
                else None,
            },
        }

    @staticmethod
    def _weekly_brief(history: pd.DataFrame) -> dict:
        if history.empty or len(history) < 5:
            return {}
        df = history.copy()
        df["_dt"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["_dt"]).set_index("_dt")
        weekly = df["close"].astype(float).resample("W-FRI").last().dropna()
        if len(weekly) < 2:
            return {}
        weeks = weekly.tail(6)
        pcts = weeks.pct_change().iloc[1:] * 100
        last_6 = [
            {
                "week": str(w.date()),
                "close": _round(float(c), 2),
                "pct_chg": _round(float(p), 2) if pd.notna(p) else None,
            }
            for (w, c), (_, p) in zip(weeks.items(), pcts.items())
        ]
        ema12 = weekly.ewm(span=12, adjust=False).mean()
        ema26 = weekly.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        trend = "周线多头" if dif.iloc[-1] > dea.iloc[-1] > 0 else (
            "周线空头" if dif.iloc[-1] < dea.iloc[-1] < 0 else "周线修复中"
        )
        return {"last_6": last_6, "macd_dif": _round(float(dif.iloc[-1]), 3), "macd_dea": _round(float(dea.iloc[-1]), 3), "trend": trend}

    @staticmethod
    def _moneyflow_brief(frame: pd.DataFrame) -> dict:
        if frame.empty:
            return {"recent": [], "net_sum_5": None}
        rows = []
        for _, r in frame.tail(10).iterrows():
            net = r["net_mf_amount"]
            rows.append(
                {
                    "date": str(r["trade_date"]),
                    "net": _round(float(net), 0) if pd.notna(net) else None,
                    "lg": _round(float((r["buy_lg_amount"] or 0) - (r["sell_lg_amount"] or 0)), 0),
                    "elg": _round(float((r["buy_elg_amount"] or 0) - (r["sell_elg_amount"] or 0)), 0),
                }
            )
        net_sum = frame.tail(5)["net_mf_amount"]
        net_sum_5 = _round(float(net_sum.sum()), 0) if not net_sum.isna().all() else None
        return {"recent": rows, "net_sum_5": net_sum_5}

    @staticmethod
    def _news_brief(frame: pd.DataFrame) -> list[dict]:
        """舆情快照:借鉴 TradingAgents-CN 的质量评估口径,输出双源结构化条目。

        每个条目带来源(source_kind / source_name)、可信度、情绪、时效、
        关联置信度;低可信度(credibility < 0.3)记录直接剔除,不喂给模型。
        这属于"过滤 + 评估"而不是新增采集器。
        """
        items = []
        for _, r in frame.iterrows():
            credibility = r.get("credibility")
            if credibility is not None and pd.notna(credibility) and float(credibility) < 0.3:
                continue  # 低可信度条目不喂给模型
            
            title = r["title"] if pd.notna(r["title"]) else ""
            source_name = r.get("source_name") if pd.notna(r.get("source_name")) else ""
            source_kind = r.get("source_kind") if pd.notna(r.get("source_kind")) else "news"
            base_cr = r.get("base_credibility")
            base_cred = _round(float(base_cr), 2) if base_cr is not None and pd.notna(base_cr) else None
            link_conf = r.get("link_confidence")
            relevance = _round(float(link_conf), 2) if link_conf is not None and pd.notna(link_conf) else None
            sentiment = r["sentiment"] if pd.notna(r["sentiment"]) else None
            sentiment_score = r.get("sentiment_score")
            sentiment_val = _round(float(sentiment_score), 3) if sentiment_score is not None and pd.notna(sentiment_score) else None
            
            # 质量评分 = 关联度 + 来源可信度 + 情绪强度(仅供参考,不绝对)
            quality = None
            if relevance is not None or base_cred is not None:
                parts = [0.0]
                if relevance is not None:
                    parts.append(relevance * 0.6)
                if base_cred is not None:
                    parts.append(base_cred * 0.4)
                quality = _round(sum(parts), 2)

            items.append(
                {
                    "title": title,
                    "source": source_name,
                    "source_kind": source_kind,
                    "published": str(r["published_at"]) if pd.notna(r["published_at"]) else None,
                    "sentiment": sentiment,
                    "sentiment_score": sentiment_val,
                    "credibility": _round(float(credibility), 2) if credibility is not None and pd.notna(credibility) else None,
                    "base_credibility": base_cred,
                    "relevance": relevance,
                    "quality_score": quality,
                }
            )
        return items[:15]

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


def _round(value: float, digits: int = 2):
    """安全取整:NaN/None 原样返回,不做 0 冒充。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return round(num, digits)


__all__ = ["AgentJudgeManager", "TASK_KIND", "STALE_AFTER_SECONDS"]
