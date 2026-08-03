"""多 agent 短线研判路由。"""

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel

from app.dependencies import get_agent_judge_manager
from app.services.agents import AgentJudgeManager

router = APIRouter()


class SingleJudgeRequest(BaseModel):
    """对单只股票发起深度研判(三位分析师+多空辩论+风控)。"""

    ts_code: str
    force: bool = False


class JudgeRequest(BaseModel):
    """发起一次多 agent 短线研判。字段全部可省略(用面板/配置默认值)。"""

    # 粗筛候选数量(1~max_candidates,默认取配置)
    candidates: int | None = None
    # 深度学习数量(1~max_depth,默认取配置)
    depth: int | None = None
    # 最终输出数量(1~max_final,默认取配置)
    final: int | None = None
    # 只研判指定股票;省略则用最近一次扫描的候选池
    ts_codes: list[str] | None = None
    # force=True 绕过"相同参数已成功"的幂等拦截,强制重跑
    force: bool = False


@router.get("/agents/status")
def agent_status(manager=Depends(get_agent_judge_manager)) -> dict:
    """多 agent 研判可用性。未启用/未配置时 availability 明确说明缺什么。"""
    return manager.status()


@router.get("/agents/candidates")
def agent_candidates(
    limit: int = Query(default=50, ge=1, le=200),
    ts_codes: list[str] = Query(default=[]),
    manager=Depends(get_agent_judge_manager),
) -> dict:
    """研判输入池预览(最近一次扫描的候选,或指定股票)。"""
    return manager.candidates(limit=limit, ts_codes=ts_codes)


@router.post("/agents/single", status_code=status.HTTP_202_ACCEPTED)
def start_single_judge(
    body: SingleJudgeRequest,
    response: Response,
    manager=Depends(get_agent_judge_manager),
) -> dict:
    """对单只股票发起深度研判,后台线程执行,返回 job_id。"""
    job = manager.start_single(ts_code=body.ts_code, force=body.force)
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return job


@router.post("/agents/judge", status_code=status.HTTP_202_ACCEPTED)
def start_judge(
    body: JudgeRequest,
    response: Response,
    manager=Depends(get_agent_judge_manager),
) -> dict:
    """发起一次研判。后台线程执行,返回 job_id;进度用 GET /api/agents/jobs/{job_id} 轮询。"""
    job = manager.start(
        candidates=body.candidates,
        depth=body.depth,
        final_count=body.final,
        ts_codes=body.ts_codes,
        force=body.force,
    )
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return job


@router.get("/agents/results")
def agent_results(
    as_of: str | None = Query(default=None, description="只返回该交易日的批次"),
    limit: int = Query(default=20, ge=1, le=100),
    manager=Depends(get_agent_judge_manager),
) -> dict:
    """已完成的研判结果(只含成功批次),可按交易日过滤。"""
    return manager.results(as_of=as_of, limit=limit)


@router.get("/agents/jobs")
def list_judge_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    manager=Depends(get_agent_judge_manager),
) -> dict:
    """最近研判批次列表(不含详细结论,结论用单个任务详情拉)。"""
    return {"items": manager.recent(limit=limit)}


@router.get("/agents/jobs/{job_id}")
def get_judge_job(job_id: str, manager=Depends(get_agent_judge_manager)) -> dict:
    """单个研判任务:状态/进度/参数/结论列表(含分析师详情)。"""
    return manager.get(job_id)
