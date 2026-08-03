from __future__ import annotations

from app.repositories.market import MarketRepository


class OverviewService:
    def __init__(self, repository: MarketRepository, scan_manager) -> None:
        self.repository = repository
        self.scan_manager = scan_manager

    def get(self) -> dict:
        latest_run = self.repository.latest_run()
        latest_scan = None
        if latest_run is not None:
            rows = self.repository.scan_rows(str(latest_run["run_id"]))
            picks = rows[rows["selected"] == True].sort_values("rank")
            latest_scan = {
                **latest_run,
                "picks": [
                    {
                        "ts_code": row.ts_code,
                        "name": row.name,
                        "industry": row.industry,
                        "rank": int(row.rank),
                        "total": float(row.total),
                        "money_class": row.money_class,
                        "one_line": row.one_line,
                    }
                    for row in picks.itertuples()
                ],
            }
        return {
            "latest_trade_date": self.repository.latest_trade_date(),
            "tables": self.repository.table_stats(),
            "latest_scan": latest_scan,
            "scan_job": self.scan_manager.current_job(),
        }
