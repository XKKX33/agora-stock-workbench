from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine.db import Store

from app.api import (
    agents,
    ai,
    analytics,
    backtest,
    experiments,
    health,
    kline,
    news,
    overview,
    pipelines,
    returns,
    reviews,
    scans,
    screener,
    settings as settings_api,
    stocks,
    watchlist,
)
from app.config import AppSettings
from app.errors import register_error_handlers
from app.repositories.market import MarketRepository
from app.services.agents import AgentJudgeManager
from app.services.experiments import ExperimentService
from app.services.news_collect import NewsCollectManager
from app.services.pipelines import PipelineManager
from app.services.returns import ReturnsService
from app.services.scans import ScanManager
from app.services.scheduler import CloseScheduler

from app.services.pi_agent import PiAgentProcessSupervisor
from engine.config import load_settings_with_local
logger = logging.getLogger(__name__)


_PAGES = {
    "index.html",
    "p1_desk.html",
    "p2_sentiment.html",
    "p3_foundry.html",
    "p4_factorlab.html",
    "p5_ledger.html",
    "p6_chart.html",
    "p7_news.html",
    "p8_ai.html",
    "p9_backtest.html",
    "p10_watchlist.html",
    "p11_agents.html",
    "p12_settings.html",
    "p13_agent_dashboard.html",
}


def migrate_schema(db_path: Path) -> bool:
    """启动时补齐缺失的表结构。返回是否执行了迁移。

    为什么放在启动而不是请求路径:读路径一律 ensure_schema=False(读请求不该
    凭空建表),但 task_runs 等新表在既有库里并不存在,读到就会抛
    CatalogException。启动时集中执行一次 DDL,读路径此后总能命中。

    数据库文件不存在时不建库:那说明还没有采过数据,凭空造一个空库会让
    "无数据"变成"有库但全空",掩盖真正的问题。
    """
    path = Path(db_path)
    if not path.exists():
        logger.warning("数据库文件不存在,跳过表结构迁移: %s", path)
        return False
    with Store(path, ensure_schema=True):
        pass
    return True


# 回收窗口：最后一次报活超过它才算残留。
#
# 原先取 0，理由写的是"DuckDB 单写者，能打开库就证明没有别的写进程"。这个推理是错的：
# Store(ensure_schema=False) 打开后立刻关闭，并不持有写锁，另一个进程的流程线程完全
# 可以正在跑。实测就撞上了——同目录起了第二个服务实例，它启动时把第一个实例正在跑的
# 研判批次收成了 failed，而那个批次还在继续调模型，管道进度和库状态从此打架。
#
# 15 分钟是这样定的：agent_runs 每完成一个角色调用就更新 heartbeat_at，20 只 × 7 角色
# 里最慢的单次调用实测在 2 分钟内；实验批次没有心跳列，按 created_at 判，而一次完整
# 一键流程约 40 分钟——所以实验批次单独放宽到 90 分钟，避免把跑到一半的流程收掉。
_AGENT_IDLE_SECONDS = 15 * 60
_EXPERIMENT_IDLE_SECONDS = 90 * 60


def reclaim_stale_runs(db_path: Path) -> None:
    """启动时收尾上一次进程留下的 running 批次。

    批次的收尾逻辑写在流程函数尾部，进程被强杀（关控制台、任务管理器结束、
    断电）就永远不执行，状态永久停在 running。库里的 running 因此无法区分
    「正在跑」和「跑它的进程早就死了」。

    判据是"多久没报活"，不是"是否 running"：同目录可能有另一个实例正在跑，
    无条件收掉会把活着的批次误判成失败。收成 failed 并写明原因是进程中断，
    而不是业务失败——两者的排查方向完全不同。
    """
    now = datetime.now(timezone.utc).isoformat()
    with Store(db_path, ensure_schema=False) as store:
        experiments_reclaimed = store.reclaim_stale_experiment_runs(
            now=now, max_idle_seconds=_EXPERIMENT_IDLE_SECONDS
        )
        agents_reclaimed = store.reclaim_stale_agent_runs(
            now=now, max_idle_seconds=_AGENT_IDLE_SECONDS
        )
        tasks_reclaimed = store.reclaim_stale_task_runs(
            now=now, max_idle_seconds=_AGENT_IDLE_SECONDS
        )
    if experiments_reclaimed:
        logger.warning(
            "回收未收尾的实验批次 %d 个: %s",
            len(experiments_reclaimed),
            ", ".join(experiments_reclaimed),
        )
    if agents_reclaimed:
        logger.warning(
            "回收未收尾的 Agent 运行 %d 个: %s",
            len(agents_reclaimed),
            ", ".join(agents_reclaimed),
        )
    if tasks_reclaimed:
        logger.warning(
            "回收未收尾的任务 %d 个: %s",
            len(tasks_reclaimed),
            ", ".join(tasks_reclaimed),
        )


def create_app(settings: AppSettings | None = None) -> FastAPI:
    app_settings = settings or AppSettings()
    repository = MarketRepository(app_settings.db_path)
    scan_manager = ScanManager(app_settings.db_path)
    pipeline_manager = PipelineManager(app_settings.db_path)
    news_collect_manager = NewsCollectManager(app_settings.db_path)
    agent_judge_manager = AgentJudgeManager(app_settings.db_path)
    experiment_service = ExperimentService(app_settings.db_path)
    returns_service = ReturnsService(app_settings.db_path)
    scheduler = CloseScheduler(pipeline_manager)
    pi_supervisor = PiAgentProcessSupervisor(app_settings.workbench_root / "pi_agent")
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        migrated = migrate_schema(app_settings.db_path)
        if migrated:
            # 上一次进程被强杀时，正在跑的批次没人收尾，会永久停在 running。
            # 此刻还没有任何流程线程启动，仍是 running 的一定是残留，直接收掉，
            # 否则看板和幂等判断都会把它当成活跃任务。
            reclaim_stale_runs(app_settings.db_path)
        app.state.pi_agent_status = {"availability": "unavailable", "reason": "Pi Agent 尚未启动"}
        try:
            settings = load_settings_with_local()
            ai = settings.get("ai") or {}
            api_key = os.environ.get(str(ai.get("api_key_env") or "WORKBENCH_AI_API_KEY"))
            if not api_key:
                raise RuntimeError("模型凭据未配置")
            handle = pi_supervisor.start(
                base_url="http://127.0.0.1:43123",
                model_api_key=api_key,
                model_base_url=str(ai.get("base_url") or "") or None,
                model_name=str(ai.get("model") or "") or None,
            )
            app.state.pi_agent_handle = handle
            app.state.pi_agent_status = {"availability": "available", "base_url": handle.base_url}
            agent_judge_manager.set_pi_agent_status(app.state.pi_agent_status, handle.client)
            agent_judge_manager.set_pi_supervisor(pi_supervisor)
            pipeline_manager.set_pi_agent_client(handle.client)
        except Exception as exc:
            app.state.pi_agent_handle = None
            reason = str(exc).strip() or "Pi Agent 启动失败"
            app.state.pi_agent_status = {"availability": "unavailable", "reason": reason}
            agent_judge_manager.set_pi_agent_status(app.state.pi_agent_status)
            pipeline_manager.set_pi_agent_client(None)
            logger.warning("Pi Agent unavailable: %s", type(exc).__name__)
        if migrated:
            scheduler.start()
        else:
            logger.warning("数据库不可用,未启动盘后调度线程")
        yield
        scheduler.stop()
        pipeline_manager.shutdown()
        news_collect_manager.shutdown()
        agent_judge_manager.shutdown()
        scan_manager.shutdown()
        pi_supervisor.close()

    app = FastAPI(
        title="AGORA Quant Workbench",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.repository = repository
    app.state.scan_manager = scan_manager
    app.state.pipeline_manager = pipeline_manager
    app.state.news_collect_manager = news_collect_manager
    app.state.agent_judge_manager = agent_judge_manager
    app.state.experiment_service = experiment_service
    app.state.returns_service = returns_service
    app.state.scheduler = scheduler
    register_error_handlers(app)

    app.include_router(health.router, prefix="/api")
    app.include_router(kline.router, prefix="/api")
    app.state.pi_agent_supervisor = pi_supervisor
    app.include_router(overview.router, prefix="/api")
    app.include_router(scans.router, prefix="/api")
    app.include_router(screener.router, prefix="/api")
    app.include_router(pipelines.router, prefix="/api")
    app.include_router(stocks.router, prefix="/api")
    app.include_router(watchlist.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(backtest.router, prefix="/api")
    app.include_router(experiments.router, prefix="/api")
    app.include_router(returns.router, prefix="/api")
    app.include_router(news.router, prefix="/api")
    app.include_router(reviews.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(ai.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")

    assets = app_settings.ui_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(app_settings.ui_root / "index.html")

    @app.get("/{page_name}", include_in_schema=False)
    def page(page_name: str) -> FileResponse:
        if page_name not in _PAGES:
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return FileResponse(Path(app_settings.ui_root) / page_name)

    return app


app = create_app()
