from fastapi import APIRouter, Depends, Response, status

from app.dependencies import get_pipeline_manager, get_scheduler
from app.schemas.pipelines import (
    PipelineAccepted,
    PipelineBackfillAccepted,
    PipelineBackfillRequest,
    PipelineRequest,
    ScheduleStatus,
)
from app.services.pipelines import TASK_KIND, TASK_KIND_BACKFILL

router = APIRouter()


@router.post(
    "/pipelines", response_model=PipelineAccepted, status_code=status.HTTP_202_ACCEPTED
)
def create_pipeline(
    body: PipelineRequest,
    response: Response,
    manager=Depends(get_pipeline_manager),
) -> PipelineAccepted:
    """手动触发盘后任务链。新建任务返回 202;命中已完成的同批次返回 200。

    命中已完成不是错误:调用方拿到的是同一 (交易日, 策略) 的既有结果,
    用 200 与"确实新排队了"区分开,避免前端一直轮询一个不会再变的任务。
    """
    job = manager.start(
        trade_date=body.trade_date,
        strategy=body.strategy,
        online=body.online,
        force=body.force,
        ignore_gate=body.ignore_gate,
    )
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return PipelineAccepted(
        job_id=job["job_id"],
        status=job["status"],
        kind=job.get("kind", TASK_KIND),
        trade_date=job.get("trade_date"),
        strategy=job.get("strategy"),
        reused=bool(job.get("reused", False)),
        gate=job.get("gate"),
    )


@router.post(
    "/pipelines/backfill",
    response_model=PipelineBackfillAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def backfill_pipelines(
    body: PipelineBackfillRequest,
    response: Response,
    manager=Depends(get_pipeline_manager),
) -> PipelineBackfillAccepted:
    """补齐最近 count 个可见交易日。新建任务返回 202;命中已完成的同批返回 200。

    路由顺序说明:这条必须排在 /pipelines/{job_id} 之前,否则 "backfill"
    会被当成 job_id 匹配掉。
    """
    job = manager.backfill(
        count=body.count,
        strategy=body.strategy,
        online=body.online,
        force=body.force,
    )
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    # 命中已完成时拿到的是库里的任务行,日期列表在 result 里
    result = job.get("result") or {}
    dates = [str(item) for item in (job.get("dates") or result.get("dates") or [])]
    return PipelineBackfillAccepted(
        job_id=job["job_id"],
        status=job["status"],
        kind=job.get("kind", TASK_KIND_BACKFILL),
        trade_date=job.get("trade_date"),
        strategy=job.get("strategy"),
        dates=dates,
        count=int(job.get("count", len(dates))),
        reused=bool(job.get("reused", False)),
    )


@router.get("/pipelines/workflow")
def pipeline_workflow(manager=Depends(get_pipeline_manager)) -> dict:
    """Fixed nine-step workflow contract for the workflow page."""
    return manager.workflow_definition()

@router.get("/pipelines/status", response_model=ScheduleStatus)
def pipeline_status(scheduler=Depends(get_scheduler)) -> dict:
    """调度状态:配置、当前闸门结论、最近一次链条任务、调度线程运行情况。

    路由顺序说明:这条必须排在 /pipelines/{job_id} 之前,否则 "status"
    会被当成 job_id 匹配掉,永远返回 404。
    """
    return scheduler.status()


@router.get("/pipelines/{job_id}")
def get_pipeline(job_id: str, manager=Depends(get_pipeline_manager)) -> dict:
    return manager.get(job_id)


@router.get("/pipelines")
def list_pipelines(
    limit: int = 20,
    kind: str | None = None,
    manager=Depends(get_pipeline_manager),
) -> dict:
    """最近的盘后任务链,供台账页展示历史复盘批次。

    kind 省略时只看一键链条;传 one_click_backfill 可以查补齐协调器的历史。
    """
    return {"items": manager.recent(limit=limit, kind=kind)}
