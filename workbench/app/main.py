from __future__ import annotations

import logging
from contextlib import asynccontextmanager
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
    health,
    kline,
    news,
    overview,
    pipelines,
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
from app.services.news_collect import NewsCollectManager
from app.services.pipelines import PipelineManager
from app.services.scans import ScanManager
from app.services.scheduler import CloseScheduler

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


def create_app(settings: AppSettings | None = None) -> FastAPI:
    app_settings = settings or AppSettings()
    repository = MarketRepository(app_settings.db_path)
    scan_manager = ScanManager(app_settings.db_path)
    pipeline_manager = PipelineManager(app_settings.db_path)
    news_collect_manager = NewsCollectManager(app_settings.db_path)
    agent_judge_manager = AgentJudgeManager(app_settings.db_path)
    scheduler = CloseScheduler(pipeline_manager)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        migrated = migrate_schema(app_settings.db_path)
        # 表结构没迁移成功(库文件不存在)时不启动调度线程:此时连
        # trade_cal 都没有,轮询只会每分钟刷一条同样的"日历缺失"。
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

    app = FastAPI(
        title="Hermes Quant Workbench",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.repository = repository
    app.state.scan_manager = scan_manager
    app.state.pipeline_manager = pipeline_manager
    app.state.news_collect_manager = news_collect_manager
    app.state.agent_judge_manager = agent_judge_manager
    app.state.scheduler = scheduler
    register_error_handlers(app)

    app.include_router(health.router, prefix="/api")
    app.include_router(kline.router, prefix="/api")
    app.include_router(overview.router, prefix="/api")
    app.include_router(scans.router, prefix="/api")
    app.include_router(screener.router, prefix="/api")
    app.include_router(pipelines.router, prefix="/api")
    app.include_router(stocks.router, prefix="/api")
    app.include_router(watchlist.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(backtest.router, prefix="/api")
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
