from fastapi import APIRouter, Query, Request

from app.errors import WorkbenchError
from app.services.backtest import BacktestService
from engine import backtest as bt

router = APIRouter()


def _check_horizon(horizon: str) -> None:
    """非法期限走统一错误体,而不是让 ValueError 变成 500。

    500 的含义是"服务端坏了",而这里是请求参数不对——混同会让排查方向跑偏。
    """
    if horizon not in bt.HORIZON_DAYS:
        raise WorkbenchError(
            "invalid_horizon",
            f"未知期限 {horizon}",
            details={"allowed": bt.horizons()},
        )


@router.get("/backtest")
def backtest(
    request: Request,
    strategy: str | None = None,
    horizon: str = "ret5",
    top_k: int = Query(default=5, ge=1, le=50),
    cost_bps: float | None = Query(default=None, ge=0, le=500),
) -> dict:
    _check_horizon(horizon)
    return BacktestService(request.app.state.repository).run(
        strategy=strategy, horizon=horizon, top_k=top_k, cost_bps=cost_bps
    )


@router.get("/backtest/compare")
def backtest_compare(
    request: Request,
    horizon: str = "ret5",
    top_k: int = Query(default=5, ge=1, le=50),
    cost_bps: float | None = Query(default=None, ge=0, le=500),
) -> dict:
    _check_horizon(horizon)
    return BacktestService(request.app.state.repository).compare(
        horizon=horizon, top_k=top_k, cost_bps=cost_bps
    )
