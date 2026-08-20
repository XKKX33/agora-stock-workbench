"""一键全流程的同步业务编排。

线程、幂等和 task_runs 由 PipelineManager 管理；本模块只按固定顺序执行
业务步骤。Agent 阶段通过正式 Pi 协议执行，不创建第二个后台任务。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from engine.agents import AgentConfig, load_agent_config
from engine.ai import AIConfig, load_ai_config
from engine.db import Store
from engine.experiments import (
    build_experiment_decisions,
    candidate_pool_hash,
    required_entry_limit_dates,
)
from engine.returns import calculate_experiment_returns
from engine.ingest_tushare import (
    TushareClient,
    confirm_latest_trade_date,
    ingest_calendar,
    ingest_daily_limits,
    ingest_snapshot,
)
from engine.run_scan import (
    ScanPreparation,
    ScanResult,
    _calendar_lookahead_end,
    _make_client,
    prepare_scan_data,
    score_prepared_scan,
    validate_scan_integrity,
)
from engine.schedule import is_trading_day, normalize_trade_date
from engine.config import load_settings_with_local, load_strategy
from engine.methodology import build_agent_brief
from engine.visibility import (
    ensure_visible,
    local_base_session,
    require_visible_as_of,
    resolve_window,
)

from app.repositories.market import MarketRepository
from app.errors import safe_error_message
from app.services.agents import AgentEventBus
from app.services.agents_data import AgentDataMixin
from app.schemas.pi_agent import PiAgentRequest, PiLimits, PiMethodology, PiModelConfig
from app.services.pi_agent import PiAgentClient

STEP_NAMES = (
    "preflight",
    "calendar",
    "market_data",
    "backfill_returns",
    "integrity",
    "scan",
    "collect_news",
    "agents",
    "persist_experiment",
)
STEP_CONTRACT = {
    "preflight": ("配置预检", ["strategy", "model", "online"]),
    "calendar": ("交易日历", ["as_of", "calendar_rows", "confirmed_rows", "visible_as_of", "base_session", "delay_sessions", "hidden_count"]),
    "market_data": ("市场数据", ["as_of", "snapshot_count", "candidate_count", "data_cutoff_at", "data_quality", "ingest_as_of"]),
    "backfill_returns": ("历史收益", ["required_limit_dates", "daily_limit_rows", "updated", "filled", "pending", "unavailable", "return_filled", "visible_as_of"]),
    "integrity": ("完整性", ["as_of", "candidate_count", "context_count"]),
    "scan": ("规则扫描", ["run_id", "as_of", "candidate_count", "scored_count", "passed_count", "rule_final_count", "candidate_hash"]),
    "collect_news": ("舆情采集", ["fetched", "stored", "duplicates"]),
    "agents": ("Agent 研判", ["candidates", "depth", "final_count"]),
    "persist_experiment": ("实验落库", ["group_counts"]),
}
STEP_DEPENDENCIES = {
    "calendar": ("preflight",),
    "market_data": ("calendar",),
    "backfill_returns": ("calendar",),
    "integrity": ("market_data",),
    "scan": ("integrity",),
    "collect_news": ("calendar",),
    "agents": ("scan",),
    "persist_experiment": ("scan",),
}
PROMPT_VERSION = "agents-v1"



@dataclass
class OneClickContext:
    run_id: str
    db_path: Path
    strategy: str
    trade_date: Optional[str]
    online: bool
    exchange: str
    settings: dict[str, Any] = field(default_factory=dict)
    strategy_config: dict[str, Any] = field(default_factory=dict)
    agent_config: Optional[AgentConfig] = None
    ai_config: Optional[AIConfig] = None
    market_client: Optional[TushareClient] = None
    pi_client: Optional[PiAgentClient] = None
    as_of: Optional[str] = None
    confirmed_market_rows: Optional[int] = None
    data_cutoff_at: Optional[str] = None
    prepared: Optional[ScanPreparation] = None
    scan_result: Optional[ScanResult] = None
    scan_rows: Optional[pd.DataFrame] = None
    agent_run_started: bool = False
    agent_run_succeeded: bool = False
    defer_experiment_commit: bool = False
    # 可见日期闸门:ingest_as_of 是当前已知最新交易日(只用于摄取与实时舆情),
    # visible_as_of 是往前退 visibility_delay 个开市日后的可见上限(选股/研判只能用它)。
    ingest_as_of: Optional[str] = None
    visible_as_of: Optional[str] = None
    visibility_delay: int = 0
    hidden_sessions: tuple[str, ...] = ()
    refresh_latest: bool = True
    collect_live_news: bool = True
    group_counts: dict[str, int] = field(default_factory=dict)
    experiment_row: Optional[dict[str, Any]] = None
    experiment_decisions: Optional[pd.DataFrame] = None
    agent_result: Optional[dict[str, Any]] = None


class _AgentDataLoader(AgentDataMixin):
    """只复用 Agent 数据装配，不创建线程或任务。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.repository = MarketRepository(db_path)


class DefaultOneClickOperations:
    """一键流程各步骤的真实实现。"""

    def preflight(self, context: OneClickContext) -> dict:
        settings = load_settings_with_local()
        strategy_config = load_strategy(context.strategy)
        agent = load_agent_config(settings)
        ai = load_ai_config(settings)
        context.settings = settings
        context.strategy_config = strategy_config
        context.agent_config = agent
        context.ai_config = agent.ai_config(ai)
        if context.online:
            context.market_client = _make_client(settings)

        with Store(context.db_path, ensure_schema=True) as store:
            store.con.execute("SELECT 1").fetchone()
        warning = None if agent.enabled else "多 Agent 研判未启用，本次将跳过 Agent 阶段"
        return {
            "_status": "warning" if warning else "ok",
            "_detail": (
                f"配置已加载：策略 {context.strategy}，Pi 模型 {context.ai_config.model}，"
                f"在线模式 {'是' if context.online else '否'}"
                + (f"；{warning}" if warning else "")
            ),
            "strategy": context.strategy,
            "model": context.ai_config.model,
            "online": context.online,
            "warnings": [warning] if warning else [],
        }

    def calendar(self, context: OneClickContext) -> dict:
        requested = (
            normalize_trade_date(context.trade_date)
            if context.trade_date is not None
            else None
        )
        calendar_rows = 0
        if context.online:
            if context.market_client is None:
                raise RuntimeError("在线更新交易日历时缺少 Tushare 客户端")
            start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
            with Store(context.db_path, ensure_schema=True) as store:
                calendar_rows = ingest_calendar(
                    store,
                    context.market_client,
                    start,
                    _calendar_lookahead_end(),
                    exchange=context.exchange,
                )

        minimum = int(context.settings["data"]["min_daily_rows"])
        with Store(context.db_path, ensure_schema=False) as store:
            # 基准日 = 当前已知最新交易日:在线向 Tushare 确认一次,离线取本地已确认交易日。
            # 即使调用方指定了 trade_date 也要算基准日,摄取与可见窗口都以它为锚。
            if context.online:
                if context.market_client is None:
                    raise RuntimeError("在线确认交易日时缺少 Tushare 客户端")
                base, confirmed_rows = confirm_latest_trade_date(
                    context.market_client, minimum
                )
            else:
                base = local_base_session(store, minimum)
                confirmed_rows = None
                if base is None:
                    raise RuntimeError("离线模式没有可用的本地交易日")
            context.ingest_as_of = base

            window = resolve_window(
                store,
                context.settings,
                exchange=context.exchange,
                base_session=base,
            )
            context.visible_as_of = window.visible_as_of
            context.visibility_delay = window.delay_sessions
            context.hidden_sessions = tuple(window.hidden_sessions)

            if requested is not None:
                open_dates = store.open_dates(context.exchange, requested, 1)
                if not is_trading_day(requested, open_dates):
                    raise ValueError(f"{requested} 不是 {context.exchange} 的交易日")
                # 历史回放是一等公民:行情、资金流、涨跌停都按 trade_date 逐日读取,
                # 是点位安全的;唯一残留边界是 stock_basic 的名称与行业只有当期状态,
                # 历史回放沿用当期分类(Tushare 数据源限制,不是可修复的 bug)。
                # 不前视由可见闸门保证:请求日必须 <= 可见日。
                actual = ensure_visible(requested, window)
            else:
                # 默认日期只能是可见日,绝不回退成基准日。
                actual = require_visible_as_of(window)

        context.as_of = actual
        # 历史日期没有权威预期行数,传 None 让数据体检按本地实际行数走。
        context.confirmed_market_rows = confirmed_rows if actual == base else None
        return {
            "_detail": (
                f"确认信号日 {actual}，可见上限 {window.visible_as_of}，"
                f"基准日 {window.base_session}"
            ),
            "as_of": actual,
            "calendar_rows": int(calendar_rows),
            "confirmed_rows": context.confirmed_market_rows,
            "visible_as_of": window.visible_as_of,
            "base_session": window.base_session,
            "delay_sessions": window.delay_sessions,
            "hidden_count": len(window.hidden_sessions),
        }

    def market_data(self, context: OneClickContext) -> dict:
        if context.as_of is None:
            raise RuntimeError("市场数据步骤缺少已确认交易日")
        latest_ingested = None
        if (
            context.online
            and context.refresh_latest
            and context.ingest_as_of
            and context.ingest_as_of != context.as_of
        ):
            # 可见日 = 基准日 - N 个交易日,只有持续把最新交易日的截面摄取进库,
            # 窗口才会往前推;否则明天可见日指向的那一天永远缺数据。
            if context.market_client is None:
                raise RuntimeError("摄取最新交易日行情时缺少 Tushare 客户端")
            with Store(context.db_path, ensure_schema=True) as store:
                latest_ingested = ingest_snapshot(
                    store, context.market_client, context.ingest_as_of
                )
        prepared = prepare_scan_data(
            strategy_name=context.strategy,
            online=context.online,
            db_path=str(context.db_path),
            settings_override=context.settings,
            client=context.market_client,
            as_of=context.as_of,
            expected_daily_rows=context.confirmed_market_rows,
        )
        quality = getattr(prepared, "data_quality", {}) or {}
        warnings: list[str] = []
        limit_quality = quality.get("daily_limit") or {}
        limit_coverage = limit_quality.get("market_coverage")
        if limit_coverage is not None and float(limit_coverage) < 1.0:
            warnings.append(
                f"daily_limit 涨跌停价覆盖率不足: {float(limit_coverage):.1%}"
            )
        if quality.get("missing_dates"):
            warnings.append(f"市场数据缺少目标日期: {quality['missing_dates']}")
        history_window = quality.get("history_window") or {}
        candidate_count = int(len(prepared.candidates))
        candidate_pool_count = int(
            quality.get("candidate_pool_count", candidate_count)
        )
        context_filter = quality.get("context_filter") or {}
        history_excluded_candidate_count = int(
            history_window.get("excluded_count", 0)
        )
        context_excluded_candidate_count = int(
            context_filter.get("excluded_count", 0)
        )
        excluded_candidate_count = max(candidate_pool_count - candidate_count, 0)
        context.strategy_config = prepared.strategy
        context.prepared = prepared
        context.data_cutoff_at = prepared.data_cutoff_at
        if history_window:
            detail = (
                f"读取 {prepared.as_of} 行情快照 {prepared.snapshot_count} 只，"
                f"候选池 {candidate_pool_count} 只，可评分 {candidate_count} 只，"
                f"历史不足排除 {history_excluded_candidate_count} 只，"
                f"其他条件排除 {context_excluded_candidate_count} 只"
            )
        else:
            detail = (
                f"读取 {prepared.as_of} 行情快照 {prepared.snapshot_count} 只，"
                f"候选池 {candidate_count} 只"
            )
        return {
            "_status": "warning" if warnings else "ok",
            "_detail": detail,
            "as_of": prepared.as_of,
            "snapshot_count": prepared.snapshot_count,
            "candidate_pool_count": candidate_pool_count,
            "candidate_count": candidate_count,
            "excluded_candidate_count": excluded_candidate_count,
            "history_excluded_candidate_count": history_excluded_candidate_count,
            "context_excluded_candidate_count": context_excluded_candidate_count,
            "data_cutoff_at": context.data_cutoff_at,
            "data_quality": prepared.data_quality,
            "ingest_as_of": context.ingest_as_of,
            "latest_ingested": latest_ingested,
            "warnings": warnings,
        }

    def backfill_returns(self, context: OneClickContext) -> dict:
        visible_as_of = context.visible_as_of
        if visible_as_of is None:
            raise RuntimeError("历史收益步骤缺少可见日期上限")
        with Store(context.db_path, ensure_schema=True) as store:
            # 隐藏窗口内的买入日现在还不可见,不该提前补权威涨跌停价。
            dates = [
                date
                for date in required_entry_limit_dates(store, context.exchange)
                if date <= visible_as_of
            ]
            daily_limit_rows = 0
            warnings: list[str] = []
            if dates:
                if not context.online or context.market_client is None:
                    warnings.append(
                        "历史实验已到买入日，但当前无法补采权威涨跌停价"
                    )
                else:
                    daily_limit_rows = ingest_daily_limits(
                        store, context.market_client, dates
                    )
            summary = calculate_experiment_returns(
                store, exchange=context.exchange, visible_max=visible_as_of
            )
        return {
            "_status": "warning" if warnings else "ok",
            "_detail": (
                f"回填收益：更新 {summary.rows_written} 条，已成交 {summary.filled}，"
                f"待成交 {summary.pending}，无法成交 {summary.unavailable}"
            ),
            "required_limit_dates": dates,
            "daily_limit_rows": int(daily_limit_rows),
            "rows_written": summary.rows_written,
            "updated": summary.rows_written,
            "filled": summary.filled,
            "pending": summary.pending,
            "unavailable": summary.unavailable,
            "return_filled": summary.filled,
            "visible_as_of": visible_as_of,
            "warnings": warnings,
        }
 
    def integrity(self, context: OneClickContext) -> dict:
        if context.prepared is None:
            raise RuntimeError("完整性检查缺少已准备的扫描数据")
        payload = validate_scan_integrity(
            context.prepared, require_complete_sources=True
        )
        agent = context.agent_config
        if agent is None:
            raise RuntimeError("完整性检查缺少 Agent 配置")
        warnings = list(payload.get("warnings") or [])
        if payload["context_count"] < agent.default_final:
            warnings.append(
                "完整股票上下文少于 Agent 默认最终数量，将按实际数量缩小"
            )
        payload["_status"] = "warning" if warnings else "ok"
        payload["warnings"] = warnings
        payload["_detail"] = (
            f"完整性检查通过：候选 {payload['candidate_count']}，"
            f"上下文 {payload['context_count']}"
        )
        return payload

    def scan(self, context: OneClickContext) -> dict:
        if context.prepared is None or context.agent_config is None:
            raise RuntimeError("扫描步骤缺少已检查的数据或 Agent 配置")
        scan = score_prepared_scan(
            context.prepared, run_id=context.run_id, record=False
        )
        agent = context.agent_config
        frozen_scored = scan.scored[: agent.default_candidates]
        rows = []
        for rank, stock in enumerate(frozen_scored, start=1):
            rows.append(
                {
                    "ts_code": stock.ts_code,
                    "name": stock.name,
                    "industry": stock.industry,
                    "rank": rank,
                    "total": float(stock.total),
                    "one_line": stock.one_line,
                    "gate_reasons_json": json.dumps(
                        stock.gate_reasons, ensure_ascii=False
                    ),
                    "contrib_json": json.dumps(stock.contrib, ensure_ascii=False),
                    "money_class": stock.money_class,
                }
            )
        scan_rows = pd.DataFrame(rows)
        if scan_rows.empty:
            raise RuntimeError("规则评分没有产生冻结候选池")

        _, _, final_count = agent.clamp(
            len(scan_rows), agent.default_depth, agent.default_final
        )
        pool_hash = candidate_pool_hash(scan_rows)
        strategy_version = hashlib.sha256(
            json.dumps(
                context.strategy_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        experiment_row = {
            "run_id": context.run_id,
            "as_of": scan.as_of,
            "data_cutoff_at": context.data_cutoff_at,
            "status": "running",
            "strategy_name": context.strategy,
            "strategy_version": strategy_version,
            "model": context.ai_config.model if context.ai_config else None,
            "temperature": agent.temperature,
            "prompt_version": PROMPT_VERSION,
            "candidate_hash": pool_hash,
            "candidate_count": int(len(scan_rows)),
            "final_count": final_count,
            "hybrid_rule_weight": 0.5,
            "hybrid_ai_weight": 0.5,
            "created_at": created_at,
            "finished_at": None,
            "error_json": None,
        }
        with Store(context.db_path, ensure_schema=True) as store:
            if not store.create_experiment_run(experiment_row):
                raise RuntimeError(f"实验批次 run_id 已存在: {context.run_id}")
        context.scan_result = scan
        context.scan_rows = scan_rows
        context.experiment_row = experiment_row
        return {
            "_detail": (
                f"规则扫描完成：打分 {scan.scored_count}，通过 {scan.passed_count}，"
                f"冻结候选 {len(scan_rows)}"
            ),
            "run_id": scan.run_id,
            "as_of": scan.as_of,
            "candidate_count": int(len(scan_rows)),
            "scored_count": scan.scored_count,
            "passed_count": scan.passed_count,
            "rule_final_count": len(scan.final),
            "candidate_hash": pool_hash,
        }

    def collect_news(self, context: OneClickContext) -> dict:
        if context.as_of is None:
            raise RuntimeError("舆情步骤缺少 as_of")
        if context.collect_live_news is False:
            # 历史补齐批次不采集当前舆情:今天的热榜不属于那一天的信息集。
            return {
                "_status": "skipped",
                "_detail": "历史补齐不采集当前舆情,避免把今天的热榜算进历史批次",
                "trade_date": context.as_of,
                "reason": "historical_backfill",
                "fetched": 0,
                "stored": 0,
                "duplicates": 0,
            }
        from engine.close_pipeline import STATUS_OK, _collect_news_step

        # 实时热榜的内容属于今天(基准日),不能挂到 20 个交易日前的历史批次上;
        # Agent 侧只读 trade_date <= as_of 的舆情,所以既不泄露未来信息也不丢数据。
        news_trade_date = context.ingest_as_of or context.as_of
        step = _collect_news_step(
            db_path=str(context.db_path),
            trade_date=news_trade_date,
            exchange=context.exchange,
            settings=context.settings,
        )
        if step.status != STATUS_OK:
            return {
                "_status": "warning",
                "_detail": f"舆情采集不可用: {step.detail}",
                "warnings": [f"舆情采集不可用: {step.detail}"],
                **step.data,
            }
        fetched = int(step.data.get("fetched") or 0)
        accepted = int(step.data.get("stored") or 0) + int(
            step.data.get("duplicates") or 0
        )
        if fetched > 0 and accepted == 0:
            return {
                "_status": "warning",
                "_detail": f"舆情采集内容全部被拒收: {step.detail}",
                "warnings": [f"舆情采集内容全部被拒收: {step.detail}"],
                **step.data,
            }
        return {
            "_status": step.status,
            "_detail": step.detail,
            **step.data,
        }

    def agents(self, context: OneClickContext) -> dict:
        if (
            context.agent_config is None
            or context.as_of is None
            or context.scan_rows is None
        ):
            raise RuntimeError("Agent 步骤缺少配置或冻结候选池")
        agent = context.agent_config
        candidate_count, depth, final_count = agent.clamp(
            min(int(len(context.scan_rows)), agent.default_candidates),
            agent.default_depth,
            agent.default_final,
        )
        client = context.pi_client
        if client is None:
            return {
                "_status": "warning",
                "_detail": "Pi Agent 不可用，本次仅保留规则与基准实验",
                "candidates": candidate_count,
                "depth": 0,
                "final_count": 0,
                "warnings": ["Pi Agent 不可用"],
            }
        if context.ai_config is None:
            raise RuntimeError("Agent 步骤缺少合并后的 AI 配置")
        ai = context.ai_config
        loader = _AgentDataLoader(context.db_path)
        candidates: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        input_warnings: list[str] = []
        for _, row in context.scan_rows.head(candidate_count).iterrows():
            code = str(row.get("ts_code") or "")
            try:
                history = loader.repository.history(row["ts_code"], context.as_of, 40)
                if history is None or history.empty:
                    raise RuntimeError(
                        f"冻结候选 {row['ts_code']} 缺少 Agent 粗筛所需历史行情"
                    )
                compact = loader._compact_row(row, history)
                score = row.get("total")
                if score is None or pd.isna(score):
                    raise RuntimeError(f"冻结候选 {row['ts_code']} 缺少有效规则评分")
                snapshot = loader._load_snapshot(row["ts_code"], context.as_of)
                stock = snapshot.get("stock") if isinstance(snapshot, dict) else None
                if not isinstance(stock, dict) or str(stock.get("ts_code") or "").upper() != str(row["ts_code"]).upper():
                    raise RuntimeError(f"完整快照 {row['ts_code']} 无效")
                candidates.append(
                    {**compact, "total": float(score), "score": float(score)}
                )
                snapshots.append({**snapshot, "ts_code": code})
            except Exception as error:
                input_warnings.append(f"{code}: {safe_error_message(error)}")
        if not candidates:
            return {
                "_status": "warning",
                "_detail": "没有可送审的完整 Agent 候选，本次仅保留规则与基准实验",
                "candidates": 0,
                "depth": 0,
                "final_count": 0,
                "warnings": input_warnings,
            }
        candidate_count, depth, final_count = agent.clamp(
            len(candidates), agent.default_depth, agent.default_final
        )
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank

        now = datetime.now(timezone.utc).isoformat()
        event_bus = AgentEventBus(context.db_path)
        with Store(context.db_path, ensure_schema=True) as store:
            store.record_agent_run(
                {
                    "run_id": context.run_id,
                    "as_of": context.as_of,
                    "status": "running",
                    "stage": "run",
                    "candidates": candidate_count,
                    "depth": depth,
                    "final_count": final_count,
                    "progress_json": json.dumps(
                        {"stage": "run", "step": 0, "total": 0, "message": "开始 Pi 研判"},
                        ensure_ascii=False,
                    ),
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "heartbeat_at": now,
                    "error_json": None,
                    "result_json": None,
                }
            )
        context.agent_run_started = True
        event_bus.publish(
            {
                "run_id": context.run_id,
                "event_type": "run.started",
                "stage": "run",
                "status": "running",
                "content": {},
                "citations": [],
            }
        )
        try:
            from app.schemas.pi_agent import compute_candidate_hash, compute_input_hash

            request = PiAgentRequest(
                protocol_version="1",
                workflow_version="1",
                mode="batch",
                trade_date=context.as_of,
                candidate_hash=compute_candidate_hash(candidates),
                input_hash=compute_input_hash(candidates, snapshots),
                limits=PiLimits(coarse=candidate_count, deep=depth, final=final_count),
                candidates=candidates,
                snapshots=snapshots,
                model=PiModelConfig(
                    provider=ai.provider or "openai-compatible",
                    model=ai.model or "",
                    reasoning_effort=ai.reasoning_effort or "low",
                    max_tokens=ai.max_tokens,
                ),
                methodology=PiMethodology(**build_agent_brief()),
            )
            pi_run_id = client.start_judgment(request, run_id=context.run_id)
            if pi_run_id != context.run_id:
                raise RuntimeError("Pi Agent 返回的 run_id 与 pipeline task_id 不一致")
            for event in client.stream_events(pi_run_id, after_seq=0):
                event_bus.publish(
                    {
                        "run_id": context.run_id,
                        "event_type": event.get("event_type", event.get("type", "pi.event")),
                        "stage": event.get("stage", ""),
                        "role": event.get("role", ""),
                        "status": "running",
                        "content": event.get("data", event),
                        "citations": event.get("citations", []),
                    }
                )
            result_model = client.get_result(pi_run_id, request)
            result = result_model.model_dump(mode="json")
            names = {
                str(item["ts_code"]): (str(item.get("name") or ""), str(item.get("industry") or ""))
                for item in candidates
            }
            rows = [
                {
                    "run_id": context.run_id,
                    "ts_code": item["ts_code"],
                    "name": names[item["ts_code"]][0],
                    "industry": names[item["ts_code"]][1],
                    "rank": item["rank"],
                    "score": item["score"],
                    "stance": item["decision"],
                    "thesis": item.get("reason") or item["bull_case"],
                    "risks": json.dumps([item["bear_case"], item["risk_control"]], ensure_ascii=False),
                    "stage_json": json.dumps(item, ensure_ascii=False),
                }
                for item in result["final"]
            ]
            if rows:
                with Store(context.db_path, ensure_schema=True) as store:
                    store.upsert_agent_judgments(pd.DataFrame(rows))
            summary = {
                "workflow_run_id": context.run_id,
                "as_of": context.as_of,
                "input_hash": request.input_hash,
                "final": result["final"],
            }
            finished = datetime.now(timezone.utc).isoformat()
            with Store(context.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    context.run_id,
                    stage="done",
                    status="succeeded",
                    finished_at=finished,
                    heartbeat_at=finished,
                    result_json=json.dumps(summary, ensure_ascii=False),
                )
            event_bus.publish(
                {
                    "run_id": context.run_id,
                    "event_type": "run.completed",
                    "stage": "done",
                    "status": "succeeded",
                    "content": summary,
                    "citations": [],
                }
            )
            context.agent_run_succeeded = True
            context.agent_result = {
                "deep": [
                    {
                        "ts_code": item["ts_code"],
                        "score": item["score"],
                        "analysts": item["analysts"],
                    }
                    for item in result["deep"]
                ],
                "final": [
                    {
                        **item,
                        "name": names[item["ts_code"]][0],
                        "industry": names[item["ts_code"]][1],
                        "thesis": item["bull_case"],
                        "stance": item["decision"],
                        "verdict": item["decision"],
                        "risks": [item["bear_case"], item["risk_control"]],
                    }
                    for item in result["final"]
                ],
            }
            actual_depth = len(result["deep"])
            actual_final = len(result["final"])
            warnings = list(input_warnings)
            if actual_depth < depth or actual_final < final_count:
                warnings.append(
                    f"Agent 实际完成深度 {actual_depth}/{depth}、最终 {actual_final}/{final_count}"
                )
        except Exception as error:
            finished = datetime.now(timezone.utc).isoformat()
            detail = {"type": type(error).__name__, "message": safe_error_message(error)}
            with Store(context.db_path, ensure_schema=True) as store:
                store.update_agent_run(
                    context.run_id,
                    stage="failed",
                    status="failed",
                    finished_at=finished,
                    heartbeat_at=finished,
                    error_json=json.dumps(detail, ensure_ascii=False),
                )
            event_bus.publish(
                {
                    "run_id": context.run_id,
                    "event_type": "run.failed",
                    "stage": "failed",
                    "status": "failed",
                    "content": detail,
                    "citations": [],
                }
            )
            return {
                "_status": "warning",
                "_detail": f"Agent 研判失败，已保留规则扫描: {detail['message']}",
                "candidates": candidate_count,
                "depth": 0,
                "final_count": 0,
                "warnings": [*input_warnings, detail["message"]],
            }
        return {
            "_status": "warning" if warnings else "ok",
            "_detail": (
                f"Agent 研判完成：送审 {candidate_count}，深度 {depth}，"
                f"最终 {actual_final}"
            ),
            "candidates": candidate_count,
            "depth": actual_depth,
            "final_count": actual_final,
            "warnings": warnings,
        }

    def persist_experiment(self, context: OneClickContext) -> dict:
        if (
            context.experiment_row is None
            or context.scan_rows is None
        ):
            raise RuntimeError("实验落库缺少实验元数据或候选池")
        warnings: list[str] = []
        if context.agent_result is None:
            warnings.append("Agent 无可用结果，仅保存规则组和基准组")
        try:
            pool_hash, decisions = build_experiment_decisions(
                context.run_id,
                context.scan_rows,
                context.agent_result,
                final_count=context.experiment_row["final_count"],
                rule_weight=context.experiment_row["hybrid_rule_weight"],
                ai_weight=context.experiment_row["hybrid_ai_weight"],
            )
        except ValueError as error:
            warnings.append(f"Agent 结果无效，仅保存规则组和基准组: {safe_error_message(error)}")
            pool_hash, decisions = build_experiment_decisions(
                context.run_id,
                context.scan_rows,
                None,
                final_count=context.experiment_row["final_count"],
                rule_weight=context.experiment_row["hybrid_rule_weight"],
                ai_weight=context.experiment_row["hybrid_ai_weight"],
            )
        if pool_hash != context.experiment_row["candidate_hash"]:
            raise RuntimeError("四组落库前候选池哈希发生变化")
        context.experiment_row["finished_at"] = datetime.now(timezone.utc).isoformat()
        context.experiment_row["error_json"] = (
            json.dumps({"warnings": warnings}, ensure_ascii=False) if warnings else None
        )
        if context.defer_experiment_commit:
            context.experiment_decisions = decisions
        else:
            with Store(context.db_path, ensure_schema=True) as store:
                store.record_experiment(context.experiment_row, decisions)
        counts = {
            str(name): int(count)
            for name, count in decisions.groupby("group_name").size().items()
        }
        context.group_counts = counts
        return {
            "_status": "warning" if warnings else "ok",
            "_detail": (
                f"可用实验组已生成并进入原子提交流程：规则 {counts.get('rule', 0)}，AI {counts.get('ai', 0)}，"
                f"混合 {counts.get('hybrid', 0)}，基准 {counts.get('benchmark', 0)}"
            ),
            "group_counts": counts,
            "warnings": warnings,
        }

class OneClickRunner:
    """按固定顺序同步执行九个业务步骤。"""

    @classmethod
    def step_contract(cls) -> list[dict]:
        """Return the public, ordered workflow contract for callers and UI."""
        return [
            {
                "name": name,
                "display_label": STEP_CONTRACT[name][0],
                "required": True,
                "blocking": False,
                "output_keys": list(STEP_CONTRACT[name][1]),
            }
            for name in STEP_NAMES
        ]
    def __init__(
        self,
        db_path: Path,
        *,
        operations: Optional[DefaultOneClickOperations] = None,
        pi_client: Optional[PiAgentClient] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.operations = operations or DefaultOneClickOperations()
        self.pi_client = pi_client
    def run(
        self,
        *,
        run_id: str,
        strategy: str,
        trade_date: Optional[str],
        online: bool,
        exchange: str,
        refresh_latest: bool = True,
        collect_live_news: bool = True,
        on_step: Optional[Callable[[dict], None]] = None,
        on_complete: Optional[
            Callable[[dict, tuple[dict[str, Any], pd.DataFrame, ScanResult]], None]
        ] = None,
    ) -> dict:
        context = OneClickContext(
            run_id=run_id,
            db_path=self.db_path,
            strategy=strategy,
            trade_date=trade_date,
            online=online,
            exchange=exchange,
            refresh_latest=refresh_latest,
            collect_live_news=collect_live_news,
            defer_experiment_commit=on_complete is not None,
            pi_client=self.pi_client,
        )
        steps: list[dict] = []
        failed_steps: set[str] = set()

        def publish_step(name: str, step: dict) -> None:
            steps.append(step)
            if name == "persist_experiment":
                context.group_counts = dict(step["data"].get("group_counts") or {})
            if on_step is None:
                return
            try:
                on_step(
                    {
                        "current_step": name,
                        "steps": list(steps),
                        "as_of": context.as_of,
                        "group_counts": dict(context.group_counts),
                        "data_cutoff_at": context.data_cutoff_at,
                    }
                )
            except Exception as error:
                detail = safe_error_message(error)
                step["status"] = "warning"
                step["detail"] = (
                    f"{step['detail']}；进度记录失败: {detail}"
                    if step["detail"]
                    else f"进度记录失败: {detail}"
                )
                step["data"].setdefault("warnings", []).append(detail)

        for name in STEP_NAMES:
            blocked_by = [
                dependency
                for dependency in STEP_DEPENDENCIES.get(name, ())
                if dependency in failed_steps
            ]
            if blocked_by:
                failed_steps.add(name)
                publish_step(
                    name,
                    {
                        "name": name,
                        "status": "skipped",
                        "detail": f"依赖步骤未完成: {', '.join(blocked_by)}",
                        "data": {"blocked_by": blocked_by},
                    },
                )
                continue
            try:
                payload = getattr(self.operations, name)(context)
                data = dict(payload or {})
                status = str(data.pop("_status", "ok"))
                detail = str(data.pop("_detail", ""))
                step = {"name": name, "status": status, "detail": detail, "data": data}
            except Exception as error:
                failed_steps.add(name)
                public_message = safe_error_message(error)
                step = {
                    "name": name,
                    "status": "warning",
                    "detail": public_message,
                    "data": {
                        "error": {
                            "type": type(error).__name__,
                            "message": public_message,
                        }
                    },
                }
            publish_step(name, step)

        def make_result() -> dict:
            warning_steps = [step for step in steps if step["status"] == "warning"]
            skipped_steps = [step for step in steps if step["status"] == "skipped"]
            result = {
                "run_id": run_id,
                "as_of": context.as_of,
                "strategy": strategy,
                "online": online,
                "current_step": steps[-1]["name"] if steps else None,
                "steps": steps,
                "group_counts": dict(context.group_counts),
                "data_cutoff_at": context.data_cutoff_at,
                "has_warnings": bool(warning_steps or skipped_steps),
                "warning_count": len(warning_steps),
                "skipped_count": len(skipped_steps),
                "warnings": [
                    {
                        "step": step["name"],
                        "detail": step["detail"],
                        "error": step["data"].get("error"),
                    }
                    for step in warning_steps
                ],
            }
            return result

        result = make_result()
        atomic_completed = False
        atomic_error_message: str | None = None
        if on_complete is not None and (
            context.experiment_row is not None
            and context.experiment_decisions is not None
            and context.scan_result is not None
        ):
            try:
                on_complete(
                    result,
                    (
                        dict(context.experiment_row),
                        context.experiment_decisions.copy(),
                        context.scan_result,
                    ),
                )
                atomic_completed = True
            except Exception as error:
                public_message = safe_error_message(error)
                atomic_error_message = public_message
                persist_step = next(
                    step for step in steps if step["name"] == "persist_experiment"
                )
                persist_step["status"] = "warning"
                persist_step["detail"] = f"实验原子提交失败: {public_message}"
                persist_step["data"]["error"] = {
                    "type": type(error).__name__,
                    "message": public_message,
                }
                result = make_result()

        if on_complete is not None:
            failed_at = datetime.now(timezone.utc).isoformat()
            error_payload = json.dumps({"warnings": result["warnings"]}, ensure_ascii=False)
            with Store(self.db_path, ensure_schema=True) as store:
                run = store.experiment_run(run_id)
                if run is not None and run["status"] not in {"succeeded", "failed"}:
                    store.fail_experiment_run(run_id, failed_at, error_payload)
            if not atomic_completed:
                if atomic_error_message is not None:
                    raise RuntimeError(
                        f"实验原子提交失败: {atomic_error_message}"
                    )
                raise RuntimeError("九步流程未产生可原子提交的实验结果")
        return result


__all__ = [
    "calculate_experiment_returns",
    "DefaultOneClickOperations",
    "OneClickContext",
    "OneClickRunner",
    "STEP_NAMES",
]
