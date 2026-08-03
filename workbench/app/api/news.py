from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel

from app.dependencies import get_news_collect_manager, get_repository
from app.services.news import DEFAULT_LIMIT, MAX_LIMIT, NewsService

router = APIRouter()


class NewsCollectRequest(BaseModel):
    """一键采集舆情。字段全部可省略。"""

    # 目标交易日 YYYYMMDD 或带横线;省略时取日历最近开市日
    trade_date: str | None = None
    # force=True 绕过"同一交易日已采成功"的幂等拦截,强制重采
    force: bool = False


@router.post("/news/collect", status_code=status.HTTP_202_ACCEPTED)
def collect_news_now(
    body: NewsCollectRequest,
    response: Response,
    manager=Depends(get_news_collect_manager),
) -> dict:
    """一键触发舆情采集(只采这一步,不牵动扫描与复盘)。

    新建任务返回 202;命中已采成功的同一交易日返回 200(reused=True),
    与"确实新排队了"区分,避免前端一直轮询一个不会再变的任务。

    采集在后台线程执行,本接口立即返回 job_id;进度用 GET /news/collect/{job_id} 轮询。
    """
    job = manager.start(trade_date=body.trade_date, force=body.force)
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return job


@router.get("/news/collect/jobs")
def list_collect_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    manager=Depends(get_news_collect_manager),
) -> dict:
    """最近的舆情采集任务,供页面展示采集历史。

    路由顺序说明:这条必须排在 /news/collect/{job_id} 之前,否则 "jobs"
    会被当成 job_id 匹配掉,永远返回 404。
    """
    return {"items": manager.recent(limit=limit)}


@router.get("/news/collect/{job_id}")
def get_collect_job(
    job_id: str, manager=Depends(get_news_collect_manager)
) -> dict:
    """查询一次舆情采集任务的状态与结果。"""
    return manager.get(job_id)


@router.get("/news/sources")
def news_sources(repository=Depends(get_repository)) -> dict:
    """已登记的舆情来源清单(含合规备注)。

    路由顺序说明:这条必须排在 /news/{...} 之类的通配路由之前。目前没有
    冲突的通配路由,但保持这个位置,后续加了也不会被吃掉。
    """
    return NewsService(repository).sources()


@router.get("/news")
def news_digest(
    trade_date: str | None = None,
    include_duplicates: bool = False,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repository=Depends(get_repository),
) -> dict:
    """某交易日的舆情列表。

    trade_date 省略时取舆情库最新一天,而不是行情最新日:行情已更新但舆情
    还没采时,用行情日期查只会得到空列表,看起来像"当天没新闻"。
    available=False 时 missing_reason 会说明缺在哪一环。
    """
    return NewsService(repository).digest(
        trade_date, include_duplicates=include_duplicates, limit=limit
    )


@router.get("/news/stocks/{ts_code}")
def news_for_stock(
    ts_code: str,
    as_of: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repository=Depends(get_repository),
) -> dict:
    """某只股票关联到的舆情。as_of 非空时只返回 <= as_of 的条目(前视纪律)。"""
    return NewsService(repository).for_stock(ts_code, as_of=as_of, limit=limit)


@router.get("/news/industries")
def news_industries_overview(
    trade_date: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repository=Depends(get_repository),
) -> dict:
    """某交易日按行业板块聚合的舆情总览(板块名 + 条数 + 情绪分布)。

    路由顺序说明:必须排在 /news/industries/{industry} 之前,否则 "industries"
    会被当成行业名匹配到单行业路由,永远返回 404。
    """
    return NewsService(repository).industry_overview(trade_date, limit=limit)


@router.get("/news/industries/{industry}")
def news_for_industry(
    industry: str,
    as_of: str | None = None,
    trade_date: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    repository=Depends(get_repository),
) -> dict:
    """某个行业关联到的舆情。

    trade_date 非空时只看指定交易日(舆情页按板块下钻);as_of 仍保留前视
    纪律语义,两者可同时使用。
    """
    return NewsService(repository).for_industry(
        industry, as_of=as_of, trade_date=trade_date, limit=limit
    )
