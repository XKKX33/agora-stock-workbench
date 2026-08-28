from typing import Optional

from pydantic import BaseModel, field_validator

from engine.config import StrategyNotFound, available_strategies


def validate_strategy(name: str) -> str:
    """策略名必须是 config/strategies 下已登记的。

    不校验的话,拼错的策略名会先起一个后台任务,跑到读配置文件那一步才
    FileNotFoundError,报错里还带服务器绝对路径。用户只想知道"名字写错了"。
    """
    if name not in available_strategies():
        raise StrategyNotFound(name, available_strategies())
    return name


def validate_optional_strategy(name: Optional[str]) -> Optional[str]:
    """None 表示沿用配置默认值,不校验;给了名字就必须存在。"""
    return name if name is None else validate_strategy(name)


class ScanRequest(BaseModel):
    strategy: str = "strong_mainup"
    online: bool = False
    record: bool = True
    # force=True 强制重跑,绕过"同一交易日同策略已成功"的幂等拦截
    force: bool = False

    _check_strategy = field_validator("strategy")(validate_strategy)


class ScanAccepted(BaseModel):
    job_id: str
    status: str
    # 业务幂等键中的交易日,便于前端展示"这一批扫的是哪天"
    trade_date: Optional[str] = None
    # reused=True 表示命中已完成的扫描,未新建任务
    reused: bool = False
