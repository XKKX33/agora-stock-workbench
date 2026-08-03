from fastapi import APIRouter, Depends, Response, status

from app.dependencies import get_scan_manager
from app.schemas.scans import ScanAccepted, ScanRequest

router = APIRouter()


@router.post("/scans", response_model=ScanAccepted, status_code=status.HTTP_202_ACCEPTED)
def create_scan(
    body: ScanRequest,
    response: Response,
    manager=Depends(get_scan_manager),
) -> ScanAccepted:
    """提交扫描。新建任务返回 202;命中已完成的同批次返回 200。

    命中已完成不是错误:调用方拿到的是同一 (交易日, 策略) 的既有结果,
    用 200 与"确实新排队了"区分开,避免前端一直轮询一个不会再变的任务。
    """
    job = manager.start(
        strategy=body.strategy,
        online=body.online,
        record=body.record,
        force=body.force,
    )
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return ScanAccepted(
        job_id=job["job_id"],
        status=job["status"],
        trade_date=job.get("trade_date"),
        reused=bool(job.get("reused", False)),
    )


@router.get("/scans/{job_id}")
def get_scan(job_id: str, manager=Depends(get_scan_manager)) -> dict:
    return manager.get(job_id)


@router.get("/scans")
def list_scans(limit: int = 20, manager=Depends(get_scan_manager)) -> dict:
    """列出最近的扫描任务,供状态查询与运行历史展示。"""
    return {"items": manager.recent(limit=limit)}
