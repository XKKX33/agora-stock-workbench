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
        buy_cost_bps: float | None = None,
        sell_cost_bps: float | None = None,
        strategy_config_hash: str | None = None,
        signal_start: str | None = None,
        signal_end: str | None = None,
        visible_cutoff: str | None = None,
        rebalance_mode: str = "non_overlap",
        limit_up_fill_policy: str = "skip",
    ) -> dict:
        """单策略回测；旧 cost_bps 在此保留为等值双边成本假设。"""
        if cost_bps is None and buy_cost_bps is None and sell_cost_bps is None:
            cost_bps = bt.DEFAULT_COST_BPS
        frame = self.repository.picks(strategy)
        result = bt.run_backtest(
            frame,
            horizon=horizon,
            strategy=strategy,
            top_k=top_k,
            cost_bps=cost_bps,
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
            strategy_config_hash=strategy_config_hash,
            signal_start=signal_start,
            signal_end=signal_end,
            visible_cutoff=visible_cutoff,
            rebalance_mode=rebalance_mode,
            limit_up_fill_policy=limit_up_fill_policy,
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
        buy_cost_bps: float | None = None,
        sell_cost_bps: float | None = None,
    ) -> dict:
        """多策略并排,使用与单策略相同的成本口径。"""
        results = bt.compare_strategies(
            self.repository.picks(None), horizon=horizon, top_k=top_k,
            cost_bps=cost_bps, buy_cost_bps=buy_cost_bps, sell_cost_bps=sell_cost_bps,
        )
        return {
            "horizon": horizon,
            "top_k": top_k,
            "cost_bps": cost_bps,
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
