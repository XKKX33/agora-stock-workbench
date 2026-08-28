from datetime import datetime
from typing import Annotated

from fastapi import Query, Request

from app.errors import WorkbenchError


# A 股代码的唯一口径。三处路由各抄一份正则,以后支持新交易所时必然漏改一处,
# 漏掉的那个端点会把合法代码当非法拒掉。
TS_CODE_PATTERN = r"^[0-9]{6}\.(SZ|SH|BJ)$"


def _validate_yyyymmdd(value: str | None, field: str) -> str | None:
    """交易日参数只接受真实存在的 YYYYMMDD。

    只靠正则会放过 20260231 这种不存在的日期,业务层随后把它当成"那天没数据",
    用户看到的是空结果而不是"日期写错了"。格式与真实性必须一起校验。
    """
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise WorkbenchError(
            "request_validation_failed",
            f"{field} 必须是真实存在的 YYYYMMDD 日期",
            status_code=422,
            details={"field": field},
        ) from exc
    return value


def validated_signal_date(
    as_of: Annotated[str | None, Query(pattern=r"^[0-9]{8}$")] = None,
) -> str | None:
    return _validate_yyyymmdd(as_of, "as_of")


def validated_trade_date(
    trade_date: Annotated[str | None, Query(pattern=r"^[0-9]{8}$")] = None,
) -> str | None:
    return _validate_yyyymmdd(trade_date, "trade_date")


def get_repository(request: Request):
    return request.app.state.repository


def get_scan_manager(request: Request):
    return request.app.state.scan_manager


def get_pipeline_manager(request: Request):
    return request.app.state.pipeline_manager


def get_news_collect_manager(request: Request):
    return request.app.state.news_collect_manager


def get_scheduler(request: Request):
    return request.app.state.scheduler


def get_experiment_service(request: Request):
    return request.app.state.experiment_service


def get_agent_judge_manager(request: Request):
    return request.app.state.agent_judge_manager



def get_returns_service(request: Request):
    return request.app.state.returns_service