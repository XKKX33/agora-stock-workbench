"""多 agent 短线研判路由。"""

import json
import queue
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.dependencies import (
    TS_CODE_PATTERN,
    get_agent_judge_manager,
    validated_signal_date,
)
from app.errors import WorkbenchError, safe_error_message
from app.schemas.agent_events import AgentEventsResponse

router = APIRouter()


class SingleJudgeRequest(BaseModel):
    """对单只股票发起深度研判(三位分析师+多空辩论+风控)。"""

    # 格式明显不对的代码不该先起后台线程、再花一次模型调用才发现。
    ts_code: str = Field(pattern=TS_CODE_PATTERN)
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
    # 选股批次身份由前端工作上下文传入,后端按此批次冻结输入。
    run_id: str | None = None
    as_of: str | None = None


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
        run_id=body.run_id,
        as_of=body.as_of,
    )
    if job.get("reused"):
        response.status_code = status.HTTP_200_OK
    return job


@router.get("/agents/results")
def agent_results(
    as_of: str | None = Depends(validated_signal_date),
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



def _sse(event: dict) -> str:
    """Encode one public event as an SSE frame."""
    event_type = str(event.get("event_type") or "message")
    seq = event.get("seq")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if seq is not None:
        lines.append(f"id: {int(seq)}")
    lines.extend((f"event: {event_type}", f"data: {payload}", ""))
    return "\n".join(lines) + "\n"


def _heartbeat() -> str:
    return "event: heartbeat\ndata: {}\n\n"


def _stream_events(manager, job_id: str, after_seq: int) -> Iterator[str]:
    """Replay persisted events, then bridge manager bus notifications to SSE."""
    cursor = after_seq
    terminal = {"run.completed", "run.failed"}
    try:
        while True:
            payload = manager.events(job_id, after_seq=cursor, limit=500)
            for event in payload.get("items", []):
                cursor = max(cursor, int(event["seq"]))
                yield _sse(event)
                if event.get("event_type") in terminal:
                    return
            task = manager.get(job_id)
            if task.get("status") in {"succeeded", "failed"}:
                return

            bus = getattr(manager, "event_bus", None)
            if bus is None:
                time.sleep(1.0)
                yield _heartbeat()
                continue

            # AgentEventBus is synchronous; bridge it to a bounded queue so
            # heartbeats remain timely while the request waits for the next event.
            notifications: queue.Queue = queue.Queue()
            subscription = bus.subscribe(job_id, after_seq=cursor)

            def pump() -> None:
                try:
                    for item in subscription:
                        notifications.put(item)
                except BaseException as exc:  # surfaced as a structured SSE error
                    notifications.put(exc)

            import threading

            worker = threading.Thread(target=pump, daemon=True)
            worker.start()
            try:
                while True:
                    try:
                        item = notifications.get(timeout=10.0)
                    except queue.Empty:
                        yield _heartbeat()
                        task = manager.get(job_id)
                        if task.get("status") in {"succeeded", "failed"}:
                            return
                        continue
                    if item is None:
                        yield _heartbeat()
                        task = manager.get(job_id)
                        if task.get("status") in {"succeeded", "failed"}:
                            return
                        continue
                    if isinstance(item, BaseException):
                        raise item
                    if int(item.get("seq", 0)) <= cursor:
                        continue
                    cursor = int(item["seq"])
                    yield _sse(item)
                    if item.get("event_type") in terminal:
                        return
            finally:
                close = getattr(bus, "close", None)
                if close is not None:
                    close(job_id)
    except GeneratorExit:
        raise
    except Exception as exc:  # never expose provider response/body in SSE
        error = {
            "code": "agent_stream_failed",
            "message": safe_error_message(exc),
        }
        yield _sse({"seq": cursor + 1, "event_type": "stream.error", "content": error})


@router.get("/agents/jobs/{job_id}/events", response_model=AgentEventsResponse)
def agent_events(
    job_id: str,
    after_seq: int = Query(default=0),
    limit: int = Query(default=500),
    manager=Depends(get_agent_judge_manager),
) -> dict:
    if after_seq < 0:
        raise WorkbenchError("invalid_after_seq", "after_seq 不能为负数", status_code=400)
    if limit < 1 or limit > 500:
        raise WorkbenchError("invalid_limit", "limit 需在 1~500 之间", status_code=400)
    # events() validates the persisted agent run and gives the standard 404 envelope.
    return manager.events(job_id, after_seq=after_seq, limit=limit)


@router.get("/agents/jobs/{job_id}/stream")
def agent_stream(
    job_id: str,
    after_seq: int = Query(default=0),
    manager=Depends(get_agent_judge_manager),
) -> StreamingResponse:
    if after_seq < 0:
        raise WorkbenchError("invalid_after_seq", "after_seq 不能为负数", status_code=400)
    manager.events(job_id, after_seq=after_seq, limit=1)
    return StreamingResponse(
        _stream_events(manager, job_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

@router.get("/agents/jobs/{job_id}")
def get_judge_job(job_id: str, manager=Depends(get_agent_judge_manager)) -> dict:
    """单个研判任务:状态/进度/参数/结论列表(含分析师详情)。"""
    return manager.get(job_id)
