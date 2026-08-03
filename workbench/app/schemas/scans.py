from typing import Optional

from pydantic import BaseModel


class ScanRequest(BaseModel):
    strategy: str = "strong_mainup"
    online: bool = False
    record: bool = True
    # force=True 强制重跑,绕过"同一交易日同策略已成功"的幂等拦截
    force: bool = False


class ScanAccepted(BaseModel):
    job_id: str
    status: str
    # 业务幂等键中的交易日,便于前端展示"这一批扫的是哪天"
    trade_date: Optional[str] = None
    # reused=True 表示命中已完成的扫描,未新建任务
    reused: bool = False
