"""回测服务:把 engine/backtest 的结果转成接口负载。

服务层只做三件事:取台账、调纯函数、组装返回。判定逻辑一律留在 engine——
"这条曲线可信吗"是领域问题,不该在 FastAPI 这一层重新实现一遍。
"""

from __future__ import annotations

from app.repositories.market import MarketRepository
from engine import backtest as bt


class BacktestService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        strategy: str | None = None,
        horizon: str = "ret5",
        top_k: int = 5,
        cost_bps: float | None = None,
    ) -> dict:
        """单策略回测。horizon 非法时由 engine 抛 ValueError,不在这里猜默认值。"""
        cost = bt.DEFAULT_COST_BPS if cost_bps is None else float(cost_bps)
        frame = self.repository.picks(strategy)
        result = bt.run_backtest(
            frame,
            horizon=horizon,
            strategy=strategy,
            top_k=top_k,
            cost_bps=cost,
        )
        payload = result.as_dict()
        payload["horizons"] = bt.horizons()
        payload["default_cost_bps"] = bt.DEFAULT_COST_BPS
        return payload

    def compare(
        self,
        *,
        horizon: str = "ret5",
        top_k: int = 5,
        cost_bps: float | None = None,
    ) -> dict:
        """多策略并排。只有一个策略时照样返回一条,不假装"无法对比"。"""
        cost = bt.DEFAULT_COST_BPS if cost_bps is None else float(cost_bps)
        results = bt.compare_strategies(
            self.repository.picks(None),
            horizon=horizon,
            top_k=top_k,
            cost_bps=cost,
        )
        return {
            "horizon": horizon,
            "top_k": top_k,
            "cost_bps": cost,
            "available": any(r.available for r in results),
            "items": [self._summary(r) for r in results],
        }

    @staticmethod
    def _summary(result: bt.BacktestResult) -> dict:
        """对比表只要摘要 + 曲线,不带逐期持仓明细——那是单策略详情页的事。"""
        payload = result.as_dict()
        return {
            "strategy": payload["strategy"],
            "available": payload["available"],
            "missing_reason": payload["missing_reason"],
            "metrics": payload["metrics"],
            "coverage": payload["coverage"],
            "drawdown": payload["drawdown"],
            "equity_curve": payload["equity_curve"],
        }
