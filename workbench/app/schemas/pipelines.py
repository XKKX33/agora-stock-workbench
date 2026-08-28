from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.scans import validate_optional_strategy


class PipelineRequest(BaseModel):
    """手动触发盘后任务链。全部字段可省略,省略时沿用 settings.schedule。"""

    # 手动补跑历史批次时指定;None 表示由闸门推断"当前该跑哪一天"
    trade_date: Optional[str] = None
    strategy: Optional[str] = None
    online: Optional[bool] = None
    # force=True 绕过"同一交易日同策略已成功"的幂等拦截
    force: bool = False
    # ignore_gate=True 只跳过"还没到运行时间";日历缺失或过期依旧拒绝运行
    ignore_gate: bool = False

    _check_strategy = field_validator("strategy")(validate_optional_strategy)


class PipelineAccepted(BaseModel):
    job_id: str
    status: str
    kind: str
    trade_date: Optional[str] = None
    strategy: Optional[str] = None
    # reused=True 表示命中已完成的批次,未新建任务
    reused: bool = False
    # 闸门结论。手动指定 trade_date 时为 None(该路径不跑闸门)
    gate: Optional[dict] = None


class PipelineBackfillRequest(BaseModel):
    """补齐最近若干个可见交易日。日期由可见窗口给出,调用方不能自己指定。"""

    # 补齐天数。上限 120 是刹车:再多就该走离线脚本,不该占着 API 线程池
    count: int = Field(default=20, ge=1, le=120)
    strategy: Optional[str] = None
    online: Optional[bool] = None
    # force=True 绕过"同一批次已成功"的幂等拦截,逐日抢占也一并强制
    force: bool = False

    _check_strategy = field_validator("strategy")(validate_optional_strategy)


class PipelineBackfillAccepted(BaseModel):
    job_id: str
    status: str
    kind: str
    # 协调器的幂等键日期 = dates 最后一天 = 当前可见日
    trade_date: Optional[str] = None
    strategy: Optional[str] = None
    # 待补齐的交易日,由旧到新
    dates: list[str]
    count: int
    # reused=True 表示命中已完成的同一批补齐,未新建任务
    reused: bool = False


class GatePayload(BaseModel):
    should_run: bool
    trade_date: Optional[str] = None
    reason: str
    detail: str


class ScheduleStatus(BaseModel):
    """调度状态。enabled=False 时依然如实上报配置与闸门,不假装在运行。"""

    enabled: bool
    run_after: str
    exchange: str
    strategy: str
    online: bool
    tick_seconds: int
    gate: GatePayload
    # 最近一次链条任务;从未跑过则为 None,由前端显示"暂无"
    latest: Optional[dict] = None
    # 调度线程是否真的在轮询。与 enabled 分开:配置开着但线程没起来是故障,
    # 必须能被看见,不能让页面误以为一切正常。
    running: bool = False
    last_tick_at: Optional[str] = None
    last_tick_detail: Optional[str] = None
    # 调度线程上一次触发失败的原因。非空即代表有故障待处理。
    last_error: Optional[str] = Field(default=None)
