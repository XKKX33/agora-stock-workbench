from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from engine.db import Store
from engine.review import build_review

from app.errors import WorkbenchError


class MarketRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def ensure_database(self) -> None:
        if not self.db_path.exists():
            raise WorkbenchError(
                "database_unavailable",
                f"DuckDB 数据库不存在: {self.db_path}",
                status_code=503,
            )

    def health(self) -> str:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            store.con.execute("SELECT 1").fetchone()
        return "ready"

    def latest_trade_date(self) -> str | None:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.latest_date()

    def table_stats(self) -> dict[str, dict[str, Any]]:
        self.ensure_database()
        date_columns = {
            "daily": "trade_date",
            "daily_basic": "trade_date",
            "moneyflow": "trade_date",
            "trade_cal": "cal_date",
            "picks": "as_of",
            "scan_runs": "as_of",
            # 舆情三表也要出现在体检里。少列一张表,页面就看不出"舆情从没采过",
            # 只会看到情绪页空着,分不清是没采还是采到 0 条。
            "news_items": "trade_date",
            "news_links": None,
            "news_sources": None,
        }
        result: dict[str, dict[str, Any]] = {}
        with Store(self.db_path, ensure_schema=False) as store:
            for table, date_column in date_columns.items():
                if date_column is None:
                    # 没有日期列的表只报行数,latest_date 明确为 None,
                    # 不拿别的列凑一个看起来像日期的值。
                    row = store.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    result[table] = {"row_count": int(row[0]), "latest_date": None}
                    continue
                row = store.con.execute(
                    f"SELECT COUNT(*), MAX({date_column}) FROM {table}"
                ).fetchone()
                result[table] = {
                    "row_count": int(row[0]),
                    "latest_date": row[1],
                }
        return result

    def latest_run(self) -> dict[str, Any] | None:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            frame = store.latest_scan_run()
        if frame.empty:
            return None
        row = frame.iloc[0].to_dict()
        row["top_industries"] = self._loads(row.pop("top_industries_json"), [])
        return row

    def scan_rows(self, run_id: str) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.scan_rows(run_id)

    def latest_scan_rows(self) -> tuple[dict[str, Any], pd.DataFrame]:
        run = self.latest_run()
        if run is None:
            raise WorkbenchError(
                "scan_not_found",
                "尚无扫描结果，请先运行扫描",
                status_code=404,
            )
        return run, self.scan_rows(str(run["run_id"]))
    def scan_batch(
        self,
        run_id: str,
        *,
        as_of: str | None = None,
        strategy: str | None = None,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        """按 run_id 严格读取扫描批次,约束不匹配时明确失败。"""
        self.ensure_database()
        try:
            with Store(self.db_path, ensure_schema=False) as store:
                return store.scan_batch(run_id, as_of=as_of, strategy=strategy)
        except WorkbenchError:
            raise
        except RuntimeError as exc:
            raise WorkbenchError("scan_not_found", str(exc), status_code=404) from exc

    def history(self, ts_code: str, as_of: str, bars: int = 120) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.history(ts_code, as_of, bars)

    def moneyflow(self, ts_code: str, as_of: str, days: int = 10) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.moneyflow_tail(ts_code, as_of, days)

    def picks(self, strategy: str | None = None) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.all_picks(strategy)

    # ------------------------------------------------------------ 舆情
    def news_sources(self) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_sources()

    def news_date_range(self) -> tuple[str | None, str | None]:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_date_range()

    def news_by_trade_date(
        self,
        trade_date: str,
        *,
        include_duplicates: bool = False,
        limit: int = 200,
    ) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_by_trade_date(
                trade_date, include_duplicates=include_duplicates, limit=limit
            )

    def news_for_link(
        self,
        *,
        link_type: str,
        link_key: str,
        as_of: str | None = None,
        trade_date: str | None = None,
        limit: int = 50,
    ) -> pd.DataFrame:
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_for_link(
                link_type=link_type,
                link_key=link_key,
                as_of=as_of,
                trade_date=trade_date,
                limit=limit,
            )

    def news_industry_summary(
        self, trade_date: str, *, limit: int = 50
    ) -> pd.DataFrame:
        """某交易日按行业板块聚合的舆情统计。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_industry_summary(trade_date, limit=limit)

    def news_unlinked_industry_count(self, trade_date: str) -> int:
        """某交易日没有任何行业关联的舆情条数。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return store.news_unlinked_industry_count(trade_date)

    def review(
        self, trade_date: str | None = None, strategy: str | None = None
    ) -> dict[str, Any]:
        """装配复盘。这是读路径,固定 backfill=False——打开页面不该写库。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            return build_review(
                store, trade_date=trade_date, strategy=strategy, backfill=False
            )



    # ------------------------------------------------------------ 自选股
    def watchlist(self) -> pd.DataFrame:
        """自选股列表,按 sort_order 升序。表缺失(旧库未迁移)返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.con.execute(
                    "SELECT ts_code, name, note, sort_order, added_at "
                    "FROM watchlist ORDER BY sort_order, added_at"
                ).df()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame(
                        columns=["ts_code", "name", "note", "sort_order", "added_at"]
                    )
                raise

    def add_watchlist(self, ts_code: str, note: str | None = None) -> bool:
        """加入自选股:股票不存在抛 404,重复添加幂等。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=True) as store:
            row = store.con.execute(
                "SELECT name FROM stock_basic WHERE ts_code = ?", [ts_code]
            ).fetchone()
            if row is None:
                raise WorkbenchError(
                    "stock_not_found", f"未找到股票 {ts_code}", status_code=404
                )
            return store.add_watchlist(ts_code, row[0], note)

    def remove_watchlist(self, ts_code: str) -> bool:
        """删除自选股,返回是否原本存在。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=True) as store:
            return store.remove_watchlist(ts_code)

    def watchlist_quotes(self) -> pd.DataFrame:
        """自选股列表 + 各自最新行情。表缺失(旧库未迁移)返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.watchlist_quotes()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame(
                        columns=[
                            "ts_code", "name", "note", "sort_order", "added_at",
                            "symbol", "industry", "last_date", "close",
                            "pct_chg", "vol", "amount",
                        ]
                    )
                raise

    def moneyflow_date_range(self) -> tuple[str | None, str | None]:
        """资金流数据覆盖区间;表缺失或没数据返回 (None, None)。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.moneyflow_date_range()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return None, None
                raise

    def moneyflow_industry_summary(
        self, as_of: str, *, limit: int = 100
    ) -> pd.DataFrame:
        """最新资金流交易日的行业聚合;表缺失返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.moneyflow_industry_summary(as_of, limit=limit)
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame(
                        columns=[
                            "industry", "stock_count", "net_mf_amount",
                            "buy_lg_amount", "sell_lg_amount",
                            "buy_elg_amount", "sell_elg_amount",
                        ]
                    )
                raise

    # ------------------------------------------------------------ 机器学习
    def ml_runs(self, limit: int = 20) -> pd.DataFrame:
        """训练记录,最新在前。表缺失返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.con.execute(
                    "SELECT * FROM ml_runs ORDER BY trained_at DESC LIMIT ?",
                    [int(limit)],
                ).df()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame()
                raise

    def ml_predictions(self, run_id: str, limit: int = 100) -> pd.DataFrame:
        """某次训练的最新截面预测,按 score 降序。表缺失返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.con.execute(
                    "SELECT * FROM ml_predictions WHERE run_id = ? "
                    "ORDER BY score DESC LIMIT ?",
                    [run_id, int(limit)],
                ).df()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame()
                raise

    # ------------------------------------------------------------ 回测
    def backtest_runs(self, limit: int = 20) -> pd.DataFrame:
        """回测运行记录,最新在前。表缺失返回空表。"""
        self.ensure_database()
        with Store(self.db_path, ensure_schema=False) as store:
            try:
                return store.con.execute(
                    "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                    [int(limit)],
                ).df()
            except Exception as exc:
                if "Catalog" in type(exc).__name__ or "does not exist" in str(exc):
                    return pd.DataFrame()
                raise

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if value is None or value == "":
            return default
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)
