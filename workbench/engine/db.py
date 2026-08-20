"""DuckDB 单文件存储层。

表设计(point-in-time 友好,均以 trade_date 为时间轴):
- stock_basic : 证券静态信息(ts_code/symbol/name/industry/market/list_date)
- daily       : 日线行情(ts_code+trade_date 主键)
- daily_basic : 每日指标(换手/量比/市值)
- moneyflow   : 资金流(事后确认字段)
- trade_cal   : 交易日历
- scan_runs   : 每次扫描的批次摘要
- scan_rows   : 每次扫描的全部候选、得分和淘汰原因
- picks       : 自动台账——每日选股快照(供事后复盘/IC 自检)
- news_sources: 舆情来源登记(含合规备注与基准可信度)
- news_items  : 舆情原文条目(标题/摘要/发布时间/抓取时间/链接/去重指纹)
- news_links  : 舆情与股票/行业的关联,每条都带匹配依据

纪律:
- 所有查询按 <= as_of 过滤,杜绝前视。
- upsert 用 DELETE+INSERT 保证幂等,重复回补不产生脏数据。
- 舆情只存来源给出的内容。缺摘要、判不出情绪就留 NULL,由上层显示"缺失",
  绝不用 0 或空字符串冒充"中性""无"。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import duckdb
import pandas as pd

from .db_agents import AgentEventMixin
from .db_experiments import ExperimentMixin
from .db_news import NewsAgentMixin
from .schema import _LEGACY_DECISION_COLUMNS, _PICKS_SCHEMA, _SCHEMA


def _is_stale(last_seen: Optional[str], now: str, stale_after_seconds: int) -> bool:
    """判断心跳是否超时。时间戳不可解析时保守返回 False(不抢占)。

    保守方向的理由:误判"僵死"会让两个进程同时写同一批数据;
    误判"存活"只是让任务多等一轮调度。前者远比后者糟。
    """
    if not last_seen:
        return False
    try:
        a = datetime.fromisoformat(last_seen)
        b = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return False
    if (a.tzinfo is None) != (b.tzinfo is None):
        return False  # naive/aware 混用无法安全相减
    return (b - a).total_seconds() > stale_after_seconds


class Store(AgentEventMixin, ExperimentMixin, NewsAgentMixin):
    """DuckDB 连接封装。用作上下文管理器,自动关闭。
    ensure_schema:
        True (默认,写路径) —— 建表(IF NOT EXISTS)后再用。
        False (读路径) —— 不执行任何 DDL,也不创建父目录。
        读请求执行建表有两个真实副作用:库路径写错时会凭空造出一个空库并
        伪装成"数据为空",以及在纯读场景引入不必要的 DDL。API 一律传 False。

    注意:此处不用 duckdb 的 read_only=True。DuckDB 不允许同一进程内以不同
    配置打开同一文件,而扫描任务在同进程线程池里持写连接,读连接若声明
    read_only 会直接抛配置冲突,反而把可用的读路径打断。因此隔离手段是
    "不执行 DDL",文件级仍为读写连接。
    """

    def __init__(self, db_path: str | Path, *, ensure_schema: bool = True):
        self.db_path = Path(db_path)
        self.ensure_schema = ensure_schema
        if ensure_schema:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.db_path.exists():
            raise FileNotFoundError(f"DuckDB 数据库不存在: {self.db_path}")
        # Windows 文件句柄竞态的健壮性重试:同进程并发打开同一 DuckDB 文件时,
        # DuckDB 偶发抛出 WinError 32(文件被占用)。这是打开瞬态,不是逻辑错误,
        # 短重试 3 次(20~50ms 退避)即可过去;连续失败仍上抛,绝不吞错。
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self.con = duckdb.connect(str(self.db_path))
                break
            except Exception as exc:  # noqa: BLE001 - 仅重试打开,失败继续上抛
                last_exc = exc
                if attempt < 2:
                    import time
                    time.sleep(0.02 * (attempt + 1) + 0.01)
        else:
            raise last_exc
        if ensure_schema:
            self.con.execute(_SCHEMA)
            self._migrate_scan_metadata()
            self._migrate_picks_pk()
            self._drop_legacy_decision_columns()
            self._sync_task_claims()

    def _migrate_scan_metadata(self) -> None:
        """Add audit columns required to identify immutable strategy batches."""
        existing = {
            str(row[0])
            for row in self.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'scan_runs'"
            ).fetchall()
        }
        for column in ("config_hash", "candidate_hash", "data_cutoff_at"):
            if column not in existing:
                self.con.execute(f"ALTER TABLE scan_runs ADD COLUMN {column} VARCHAR")

    def _migrate_picks_pk(self) -> None:
        """把 picks 旧主键 (run_date, strategy, ts_code) 迁移为业务幂等键 (as_of, strategy, ts_code)。

        同一个 as_of 横截面可能被多次运行写入(run_date 不同),旧主键对
        "同一横截面只保留一份"没有约束力,evaluate 统计会因此重复计票,
        污染 IC/胜率口径。迁移按 (as_of, strategy, ts_code) 分组去重,
        保留最新 run_date 的行;异 as_of 的数据互不干扰,原样保留。
        全程一个事务,任一步失败回滚,不留下半迁移状态。
        """
        row = self.con.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'picks' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()
        if row is None or "as_of" in (row[0] or []):
            return
        self.con.execute("BEGIN TRANSACTION")
        try:
            self.con.execute("ALTER TABLE picks RENAME TO picks_pk_old")
            self.con.execute(_PICKS_SCHEMA)
            self.con.execute(
                """
                INSERT INTO picks
                SELECT * FROM (
                    SELECT * FROM picks_pk_old
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY as_of, strategy, ts_code
                        ORDER BY run_date DESC
                    ) = 1
                )
                """
            )
            self.con.execute("DROP TABLE picks_pk_old")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def _drop_legacy_decision_columns(self) -> None:
        """删掉 experiment_decisions 上早已没人回填的成交/收益列。

        成交与收益现在只落 experiment_returns（一个 horizon 一行、可单独重试）。
        这些旧列在生产链路里最后一个写入方被移除后就永远是 NULL,留着只会让
        台账页显示一片空白的"legacy"列。DuckDB 的 DROP COLUMN 是元数据操作,
        不重写数据;整个迁移一个事务,任一列失败就回滚,不留半迁移状态。
        """
        existing = {
            row[0]
            for row in self.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'experiment_decisions'"
            ).fetchall()
        }
        stale = [column for column in _LEGACY_DECISION_COLUMNS if column in existing]
        if not stale:
            return
        self.con.execute("BEGIN TRANSACTION")
        try:
            for column in stale:
                self.con.execute(
                    f"ALTER TABLE experiment_decisions DROP COLUMN {column}"
                )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def _sync_task_claims(self) -> None:
        """为升级前的任务补业务键指针；每个键只指向最近一条记录。"""
        rows = self.con.execute(
            """
            SELECT task_id, kind, trade_date, COALESCE(strategy, '')
            FROM task_runs
            WHERE kind IS NOT NULL AND trade_date IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY kind, trade_date, COALESCE(strategy, '')
                ORDER BY created_at DESC, task_id DESC
            ) = 1
            """
        ).fetchall()
        for task_id, kind, trade_date, strategy_key in rows:
            self.con.execute(
                """
                INSERT INTO task_claims (kind, trade_date, strategy_key, task_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                [kind, trade_date, strategy_key, task_id],
            )

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 写入
    def upsert(self, table: str, df: pd.DataFrame, keys: Iterable[str]) -> int:
        """幂等 upsert:先按 keys 删除交集,再插入。返回写入行数。"""
        if df is None or df.empty:
            return 0
        cols = self._table_cols(table)
        use = [c for c in df.columns if c in cols]
        d = df[use].copy()
        owns_transaction = not self._has_active_transaction()
        registered = False
        try:
            if owns_transaction:
                self.con.execute("BEGIN TRANSACTION")
            self.con.register("_stg", d)
            registered = True
            key_list = [k for k in keys if k in use]
            if key_list:
                on = " AND ".join(f"t.{k} = s.{k}" for k in key_list)
                self.con.execute(
                    f"DELETE FROM {table} t USING _stg s WHERE {on}"
                )
            collist = ", ".join(use)
            self.con.execute(
                f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _stg"
            )
            if owns_transaction:
                self.con.execute("COMMIT")
        except Exception:
            if owns_transaction:
                self.con.execute("ROLLBACK")
            raise
        finally:
            if registered:
                self.con.unregister("_stg")
        return len(d)

    def _table_cols(self, table: str) -> set[str]:
        rows = self.con.execute(f"PRAGMA table_info('{table}')").fetchall()
        return {r[1] for r in rows}

    # ------------------------------------------------------------ 读取
    def latest_confirmed_date(self, min_rows: int) -> Optional[str]:
        """本地已入库、当日行数 > min_rows 的最大 trade_date。"""
        row = self.con.execute(
            """
            SELECT trade_date FROM daily
            GROUP BY trade_date HAVING COUNT(*) > ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            [min_rows],
        ).fetchone()
        return row[0] if row else None

    def latest_date(self) -> Optional[str]:
        """本地已入库的最大 trade_date(不校验行数)。离线/小样本回退用。"""
        row = self.con.execute(
            "SELECT trade_date FROM daily ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def snapshot(self, as_of: str) -> pd.DataFrame:
        """某交易日的截面快照(daily ⨝ basic ⨝ daily_basic)。"""
        return self.con.execute(
            """
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                   d.pre_close, d.pct_chg, d.vol, d.amount,
                   b.symbol, b.name, b.area, b.industry, b.market, b.list_date,
                   db.turnover_rate, db.volume_ratio, db.total_mv, db.circ_mv
            FROM daily d
            LEFT JOIN stock_basic b ON d.ts_code = b.ts_code
            LEFT JOIN daily_basic db ON d.ts_code = db.ts_code AND d.trade_date = db.trade_date
            WHERE d.trade_date = ?
            """,
            [as_of],
        ).df()

    def history(self, ts_code: str, as_of: str, bars: int) -> pd.DataFrame:
        """单票 <= as_of 的最近 bars 根日线(升序)。"""
        return self.con.execute(
            """
            SELECT * FROM (
                SELECT ts_code, trade_date, open, high, low, close, pre_close,
                       pct_chg, vol, amount
                FROM daily
                WHERE ts_code = ? AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT ?
            ) ORDER BY trade_date ASC
            """,
            [ts_code, as_of, bars],
        ).df()

    def moneyflow_tail(self, ts_code: str, as_of: str, days: int) -> pd.DataFrame:
        return self.con.execute(
            """
            SELECT * FROM (
                SELECT ts_code, trade_date, net_mf_amount, buy_lg_amount,
                       sell_lg_amount, buy_elg_amount, sell_elg_amount
                FROM moneyflow
                WHERE ts_code = ? AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT ?
            ) ORDER BY trade_date ASC
            """,
            [ts_code, as_of, days],
        ).df()

    # ------------------------------------------------------------ 自选股
    def add_watchlist(
        self, ts_code: str, name: str, note: str | None = None
    ) -> bool:
        """加入自选股:已存在保持原顺序(幂等),新加入追加到末尾。"""
        existed = self.con.execute(
            "SELECT 1 FROM watchlist WHERE ts_code = ?", [ts_code]
        ).fetchone()
        if existed is not None:
            return False
        order = int(
            self.con.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM watchlist"
            ).fetchone()[0]
        )
        from datetime import datetime, timezone

        added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.con.execute(
            "INSERT INTO watchlist (ts_code, name, note, sort_order, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [ts_code, name, note, order, added_at],
        )
        return True

    def remove_watchlist(self, ts_code: str) -> bool:
        """删除自选股,返回是否原本存在。"""
        existed = self.con.execute(
            "SELECT 1 FROM watchlist WHERE ts_code = ?", [ts_code]
        ).fetchone()
        self.con.execute("DELETE FROM watchlist WHERE ts_code = ?", [ts_code])
        return existed is not None

    def watchlist_quotes(self) -> pd.DataFrame:
        """自选股 + 各自最新一根日线。无日线的股票保留行,行情字段为 NULL。"""
        return self.con.execute(
            """
            SELECT w.ts_code, w.name, w.note, w.sort_order, w.added_at,
                   b.symbol, b.industry,
                   d.trade_date AS last_date, d.close, d.pct_chg, d.vol, d.amount
            FROM watchlist w
            LEFT JOIN stock_basic b ON b.ts_code = w.ts_code
            LEFT JOIN (
                SELECT ts_code, trade_date, close, pct_chg, vol, amount,
                       ROW_NUMBER() OVER (
                           PARTITION BY ts_code ORDER BY trade_date DESC
                       ) AS _rn
                FROM daily
            ) d ON d.ts_code = w.ts_code AND d._rn = 1
            ORDER BY w.sort_order, w.added_at
            """
        ).df()

    # ------------------------------------------------------------ 行业资金流
    def moneyflow_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """资金流数据覆盖的交易日区间;两端为空表示还没采过。"""
        row = self.con.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM moneyflow"
        ).fetchone()
        if not row or row[0] is None:
            return None, None
        return row[0], row[1]

    def moneyflow_industry_summary(
        self, as_of: str, *, limit: int = 100
    ) -> pd.DataFrame:
        """某交易日按行业板块聚合的资金流向,按净流入降序。

        只统计当天有资金流记录的股票;行业取自 stock_basic,取不到的
        归为 industry 为 NULL 的一组,由上层如实展示为"未知"。
        """
        return self.con.execute(
            """
            SELECT b.industry AS industry,
                   COUNT(*) AS stock_count,
                   SUM(m.net_mf_amount) AS net_mf_amount,
                   SUM(m.buy_lg_amount) AS buy_lg_amount,
                   SUM(m.sell_lg_amount) AS sell_lg_amount,
                   SUM(m.buy_elg_amount) AS buy_elg_amount,
                   SUM(m.sell_elg_amount) AS sell_elg_amount
            FROM moneyflow m
            JOIN stock_basic b ON b.ts_code = m.ts_code
            WHERE m.trade_date = ?
            GROUP BY b.industry
            ORDER BY net_mf_amount DESC
            LIMIT ?
            """,
            [as_of, int(limit)],
        ).df()

    def open_dates(self, exchange: str, end: str, n: int) -> list[str]:
        rows = self.con.execute(
            """
            SELECT cal_date FROM trade_cal
            WHERE exchange = ? AND is_open = 1 AND cal_date <= ?
            ORDER BY cal_date DESC LIMIT ?
            """,
            [exchange, end, n],
        ).fetchall()
        return sorted(r[0] for r in rows)

    def sessions_after(self, exchange: str, start: str, n: int) -> Optional[str]:
        """返回 start 之后(不含 start)第 n 个开市日;不足 n 个则 None。"""
        rows = self.con.execute(
            """
            SELECT cal_date FROM trade_cal
            WHERE exchange = ? AND is_open = 1 AND cal_date > ?
            ORDER BY cal_date ASC LIMIT ?
            """,
            [exchange, start, n],
        ).fetchall()
        return rows[n - 1][0] if len(rows) >= n else None

    def close_on(self, ts_code: str, trade_date: str) -> Optional[float]:
        """某票某交易日的收盘价(基准价);本地无该行返回 None。"""
        row = self.con.execute(
            "SELECT close FROM daily WHERE ts_code = ? AND trade_date = ?",
            [ts_code, trade_date],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def record_picks(self, df: pd.DataFrame) -> int:
        """按业务键 (as_of, strategy) 整组替换写入选股台账。

        表主键就是业务幂等键 (as_of, strategy, ts_code):as_of 是数据日期,
        run_date 只是墙钟执行日。周五收盘后跑一次、周六早上
        又跑一次,as_of 相同而 run_date 不同,若按 run_date 区分会把同一
        观测记两行,evaluate() 按 as_of 分组算横截面 IC,重复行会让
        同一观测被计两次,虚增 n_samples 并扭曲 IC / 胜率 / 分层收益。

        因此这里以 (as_of, strategy) 为幂等作用域:先删同作用域旧行,再插入。
        副作用:该作用域内已回填的 retN 会被清空。可接受——retN 是派生量,
        下一次 backfill_returns 会按同一口径重算;而重跑本就意味着该
        as_of 的选股集合可能变化,保留旧收益反而会张冠李戴。
        """
        if df is None or df.empty:
            return 0
        cols = self._table_cols("picks")
        use = [c for c in df.columns if c in cols]
        d = df[use].copy()
        if "as_of" not in use or "strategy" not in use:
            # 缺业务键时退回主键语义,至少保证不重复插入同一 run_date
            return self.upsert("picks", d, keys=("run_date", "strategy", "ts_code"))
        self.con.register("_stg_picks", d)
        try:
            self.con.execute(
                """
                DELETE FROM picks t USING _stg_picks s
                WHERE t.as_of = s.as_of AND t.strategy = s.strategy
                """
            )
            collist = ", ".join(use)
            self.con.execute(
                f"INSERT INTO picks ({collist}) SELECT {collist} FROM _stg_picks"
            )
        finally:
            self.con.unregister("_stg_picks")
        return len(d)

    def _replace_picks_in_transaction(
        self,
        as_of: str,
        strategy: str,
        df: pd.DataFrame,
    ) -> int:
        self.con.execute(
            "DELETE FROM picks WHERE as_of = ? AND strategy = ?",
            [as_of, strategy],
        )
        if df is None or df.empty:
            return 0
        if "as_of" not in df.columns or "strategy" not in df.columns:
            raise ValueError("选股结果缺少 as_of 或 strategy")
        if set(df["as_of"].astype(str)) != {as_of}:
            raise ValueError("选股结果混入其他 as_of")
        if set(df["strategy"].astype(str)) != {strategy}:
            raise ValueError("选股结果混入其他 strategy")
        cols = self._table_cols("picks")
        use = [column for column in df.columns if column in cols]
        staged = df[use].copy()
        self.con.register("_replace_picks_stage", staged)
        try:
            column_list = ", ".join(use)
            self.con.execute(
                f"INSERT INTO picks ({column_list}) "
                f"SELECT {column_list} FROM _replace_picks_stage"
            )
        finally:
            self.con.unregister("_replace_picks_stage")
        return len(staged)

    def replace_picks(
        self,
        as_of: str,
        strategy: str,
        df: pd.DataFrame,
    ) -> int:
        """整组替换指定信号日的选股，空结果也会清除旧行。"""
        owns_transaction = not self._has_active_transaction()
        try:
            if owns_transaction:
                self.con.execute("BEGIN TRANSACTION")
            count = self._replace_picks_in_transaction(as_of, strategy, df)
            if owns_transaction:
                self.con.execute("COMMIT")
            return count
        except Exception:
            if owns_transaction:
                self.con.execute("ROLLBACK")
            raise

    def _record_scan_in_transaction(self, run_row: dict, rows: pd.DataFrame) -> None:
        run_id = run_row.get("run_id")
        if run_row.get("as_of") is not None and run_row.get("strategy") is not None:
            stale = self.con.execute(
                "SELECT run_id FROM scan_runs WHERE as_of = ? AND strategy = ? "
                "AND COALESCE(config_hash, '') = COALESCE(?, '') AND run_id <> ?",
                [run_row.get("as_of"), run_row.get("strategy"), run_row.get("config_hash"), run_id],
            ).fetchall()
            for (old_run_id,) in stale:
                self.con.execute("DELETE FROM scan_rows WHERE run_id = ?", [old_run_id])
                self.con.execute("DELETE FROM scan_runs WHERE run_id = ?", [old_run_id])
        self.upsert("scan_runs", pd.DataFrame([run_row]), keys=("run_id",))
        self.upsert("scan_rows", rows, keys=("run_id", "ts_code"))
    def record_scan(self, run_row: dict, rows: pd.DataFrame) -> None:
        """原子写入扫描批次与全部候选明细,并清理同业务键的旧批次。

        为什么不只按 run_id upsert:run_id 每次扫描都新生成 uuid,按它 upsert
        永远命中不到旧行。同一交易日重跑 N 次就在 scan_runs / scan_rows 里
        堆 N 个批次,页面"最新扫描"取到哪个取决于 run_date 字符串排序,
        且 scan_rows 行数随重跑线性膨胀。

        因此幂等作用域取 (as_of, strategy),与 picks 一致:先删该作用域下
        所有旧批次(含其明细行),再插入本次。这样"同一交易日重复运行"
        在库里只留最后一次结果,不产生重复批次。
        """
        owns_transaction = not self._has_active_transaction()
        try:
            if owns_transaction:
                self.con.execute("BEGIN TRANSACTION")
            self._record_scan_in_transaction(run_row, rows)
            if owns_transaction:
                self.con.execute("COMMIT")
        except Exception:
            if owns_transaction:
                self.con.execute("ROLLBACK")
            raise

    def scan_runs(self, strategy: Optional[str] = None) -> pd.DataFrame:
        """按时间倒序读取扫描批次。"""
        if strategy:
            return self.con.execute(
                "SELECT * FROM scan_runs WHERE strategy = ? ORDER BY run_date DESC",
                [strategy],
            ).df()
        return self.con.execute(
            "SELECT * FROM scan_runs ORDER BY run_date DESC"
        ).df()

    def latest_scan_run(self, strategy: Optional[str] = None) -> pd.DataFrame:
        """读取最近一次扫描批次。"""
        if strategy:
            return self.con.execute(
                "SELECT * FROM scan_runs WHERE strategy = ? ORDER BY run_date DESC LIMIT 1",
                [strategy],
            ).df()
        return self.con.execute(
            "SELECT * FROM scan_runs ORDER BY run_date DESC LIMIT 1"
        ).df()

    def scan_rows(self, run_id: str) -> pd.DataFrame:
        """读取某次扫描的全部候选明细。"""
        return self.con.execute(
            "SELECT * FROM scan_rows WHERE run_id = ? ORDER BY rank ASC",
            [run_id],
        ).df()
    def scan_batch(
        self,
        run_id: str,
        *,
        as_of: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> tuple[dict, pd.DataFrame]:
        """按明确批次身份读取摘要及明细,绝不回退最近批次。"""
        if not str(run_id).strip():
            raise RuntimeError("扫描批次 run_id 不能为空")
        row = self.con.execute(
            "SELECT * FROM scan_runs WHERE run_id = ?", [str(run_id)]
        ).fetchone()
        if row is None:
            raise RuntimeError(f"扫描批次 {run_id} 不存在")
        columns = [item[0] for item in self.con.description]
        run = dict(zip(columns, row))
        if as_of is not None and str(run.get("as_of")) != str(as_of):
            raise RuntimeError(
                f"扫描批次 {run_id} 日期不匹配: 期望 {as_of}, 实际 {run.get('as_of')}"
            )
        if strategy is not None and str(run.get("strategy")) != str(strategy):
            raise RuntimeError(
                f"扫描批次 {run_id} 策略不匹配: 期望 {strategy}, 实际 {run.get('strategy')}"
            )
        if str(run.get("status") or "").lower() == "failed":
            raise RuntimeError(f"扫描批次 {run_id} 已失败")
        return run, self.scan_rows(str(run_id))

    def open_picks_awaiting_return(self, horizon_col: str) -> pd.DataFrame:
        """尚未回填某期收益的历史选股(供自动复盘补算)。"""
        return self.con.execute(
            f"SELECT run_date, as_of, strategy, ts_code, name, industry, total "
            f"FROM picks WHERE {horizon_col} IS NULL"
        ).df()

    def future_close(
        self, ts_code: str, as_of: str, n: int, exchange: str = "SSE"
    ) -> Optional[float]:
        """as_of 之后**市场**第 n 个交易日、该票的收盘价;不可得则 None。

        口径要点(与 retN 定义绑死):
        - "第 n 个交易日"按 trade_cal 的市场日历数,**不是**按该票自己的 K 线数。
          若按自身 K 线数,停牌 3 天的票 "第5根" 实为市场第 8 日,
          retN 在不同股票间口径不一致,IC/胜率统计会被污染。
        - 日历不足 n 个开市日(未来未发生 / 日历未回补) -> None。
        - 目标交易日该票无行(停牌、缺数据、退市) -> None。
          停牌不按"最近可得收盘"顶替:那等于臆造一个不可成交的收益。
          一律保持 NULL,由 pending 计数如实暴露。
        """
        target = self.sessions_after(exchange, as_of, n)
        if target is None:
            return None
        return self.close_on(ts_code, target)

    def calendar_max(self, exchange: str = "SSE") -> Optional[str]:
        """交易日历已覆盖的最大开市日。用于判断"日历是否需要回补"。"""
        row = self.con.execute(
            "SELECT MAX(cal_date) FROM trade_cal WHERE exchange = ? AND is_open = 1",
            [exchange],
        ).fetchone()
        return row[0] if row and row[0] else None

    def update_pick_return(
        self, as_of: str, strategy: str, ts_code: str, col: str, value: float
    ) -> None:
        """回填单条选股的某期收益列(col ∈ ret1/ret3/ret5/ret10)。

        用业务键 (as_of, strategy, ts_code) 定位观测:run_date 是墙钟执行日,
        不标识观测,同一 run_date 下可能并存不同 as_of 的行。
        """
        if col not in ("ret1", "ret3", "ret5", "ret10"):
            raise ValueError(f"非法收益列: {col}")
        self.con.execute(
            f"UPDATE picks SET {col} = ? "
            "WHERE as_of = ? AND strategy = ? AND ts_code = ?",
            [value, as_of, strategy, ts_code],
        )

    # ------------------------------------------------------------ 任务状态
    def claim_task(
        self,
        *,
        task_id: str,
        kind: str,
        trade_date: str,
        strategy: Optional[str],
        now: str,
        stale_after_seconds: int = 3600,
        force: bool = False,
    ) -> tuple[bool, Optional[dict]]:
        """用业务键锁表登记任务，返回 (是否抢到, 冲突任务或 None)。

        幂等作用域 = (kind, trade_date, strategy)。语义:
        - 已有 succeeded 记录  -> 不抢(除非 force),让调用方跳过。
          这就是"同一交易日重复运行不产生重复复盘"的落点。
        - 已有 queued/running 且心跳未超时 -> 不抢,防跨进程并发重复写。
        - 已有 queued/running 但心跳超时(进程崩溃遗留) -> 判为僵死,允许抢占。
        - 已有 failed -> 允许重试。

        `task_claims` 的复合主键是数据库级唯一约束；两个独立连接同时抢占时，
        只有一个能把锁行指向自己的 task_id。
        """
        strategy_key = strategy or ""
        for attempt in range(5):
            started = False
            try:
                self.con.execute("BEGIN TRANSACTION")
                started = True
                pointer = self.con.execute(
                    """
                    SELECT task_id FROM task_claims
                    WHERE kind = ? AND trade_date = ? AND strategy_key = ?
                    """,
                    [kind, trade_date, strategy_key],
                ).fetchone()
                existing = None
                if pointer is None:
                    inserted = self.con.execute(
                        """
                        INSERT INTO task_claims
                            (kind, trade_date, strategy_key, task_id)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        RETURNING task_id
                        """,
                        [kind, trade_date, strategy_key, task_id],
                    ).fetchone()
                    if inserted is None:
                        self.con.execute("ROLLBACK")
                        started = False
                        continue
                else:
                    existing = self.con.execute(
                        """
                        SELECT task_id, status, created_at, started_at, finished_at,
                               heartbeat_at, result_json, error_json
                        FROM task_runs WHERE task_id = ?
                        """,
                        [pointer[0]],
                    ).fetchone()
                    if existing is None:
                        raise RuntimeError("task_claims 指向不存在的任务")

                if existing is not None and not force:
                    status = existing[1]
                    conflict = {
                        "task_id": existing[0],
                        "status": status,
                        "created_at": existing[2],
                        "started_at": existing[3],
                        "finished_at": existing[4],
                        "heartbeat_at": existing[5],
                        "result_json": existing[6],
                        "error_json": existing[7],
                    }
                    if status == "succeeded" or (
                        status in ("queued", "running")
                        and not _is_stale(
                            existing[5] or existing[2], now, stale_after_seconds
                        )
                    ):
                        self.con.execute("COMMIT")
                        started = False
                        return False, conflict
                    if status in ("queued", "running"):
                        stale_error = (
                            '{"type":"StaleTask",'
                            '"message":"心跳超时,判定进程已崩溃"}'
                        )
                        self.con.execute(
                            """
                            UPDATE task_runs
                            SET status = 'failed', finished_at = ?,
                                error_json = ?
                            WHERE task_id = ?
                            """,
                            [
                                now,
                                stale_error,
                                existing[0],
                            ],
                        )
                        self.con.execute(
                            """
                            UPDATE experiment_runs
                            SET status = 'failed', finished_at = ?, error_json = ?
                            WHERE run_id = ? AND status IN ('queued', 'running')
                            """,
                            [now, stale_error, existing[0]],
                        )

                if existing is not None:
                    updated = self.con.execute(
                        """
                        UPDATE task_claims SET task_id = ?
                        WHERE kind = ? AND trade_date = ? AND strategy_key = ?
                          AND task_id = ?
                        RETURNING task_id
                        """,
                        [task_id, kind, trade_date, strategy_key, existing[0]],
                    ).fetchone()
                    if updated is None:
                        self.con.execute("ROLLBACK")
                        started = False
                        continue

                self.con.execute(
                    """
                    INSERT INTO task_runs
                        (task_id, kind, trade_date, strategy, status,
                         created_at, started_at, finished_at, heartbeat_at,
                         result_json, error_json)
                    VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, ?, NULL, NULL)
                    """,
                    [task_id, kind, trade_date, strategy, now, now],
                )
                self.con.execute("COMMIT")
                started = False
                return True, None
            except (duckdb.TransactionException, duckdb.ConstraintException):
                if started:
                    try:
                        self.con.execute("ROLLBACK")
                    except duckdb.TransactionException:
                        # 提交期唯一键冲突会由 DuckDB 自动结束失败事务。
                        pass
                if attempt == 4:
                    raise
        raise RuntimeError("任务抢占重试耗尽")

    def _rebind_task_claim_in_transaction(
        self, task_id: str, trade_date: str
    ) -> None:
        task = self.con.execute(
            "SELECT kind, trade_date, COALESCE(strategy, '') FROM task_runs "
            "WHERE task_id = ?",
            [task_id],
        ).fetchone()
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")
        kind, old_date, strategy_key = task
        if old_date == trade_date:
            return
        target = self.con.execute(
            """
            SELECT c.task_id, r.status
            FROM task_claims c JOIN task_runs r ON r.task_id = c.task_id
            WHERE c.kind = ? AND c.trade_date = ? AND c.strategy_key = ?
            """,
            [kind, trade_date, strategy_key],
        ).fetchone()
        if target is not None and target[0] != task_id and target[1] in (
            "queued", "running", "succeeded"
        ):
            raise ValueError(f"真实业务日期已被任务占用: {trade_date}")
        if target is None:
            self.con.execute(
                "INSERT INTO task_claims VALUES (?, ?, ?, ?)",
                [kind, trade_date, strategy_key, task_id],
            )
        else:
            self.con.execute(
                "UPDATE task_claims SET task_id = ? "
                "WHERE kind = ? AND trade_date = ? AND strategy_key = ?",
                [task_id, kind, trade_date, strategy_key],
            )
        self.con.execute(
            "DELETE FROM task_claims WHERE kind = ? AND trade_date = ? "
            "AND strategy_key = ? AND task_id = ?",
            [kind, old_date, strategy_key, task_id],
        )

    def mark_task_running(self, task_id: str, now: str) -> None:
        updated = self.con.execute(
            "UPDATE task_runs SET status='running', started_at=?, heartbeat_at=? "
            "WHERE task_id = ? AND status='queued' RETURNING task_id",
            [now, now, task_id],
        ).fetchone()
        if updated is not None:
            return
        current = self.con.execute(
            "SELECT status FROM task_runs WHERE task_id = ?", [task_id]
        ).fetchone()
        if current is None:
            raise KeyError(f"任务不存在: {task_id}")
        raise ValueError(f"任务不可改为运行中: {task_id}/{current[0]}")

    def task_heartbeat(self, task_id: str, now: str) -> None:
        self.con.execute(
            "UPDATE task_runs SET heartbeat_at = ? WHERE task_id = ?", [now, task_id]
        )

    def update_task_progress(
        self, task_id: str, now: str, result_json: str
    ) -> None:
        """同时保存心跳和可恢复的进行中结果。"""
        updated = self.con.execute(
            """
            UPDATE task_runs
            SET heartbeat_at = ?, result_json = ?
            WHERE task_id = ? AND status = 'running'
            RETURNING task_id
            """,
            [now, result_json, task_id],
        ).fetchone()
        if updated is None:
            raise KeyError(f"运行中的任务不存在: {task_id}")

    def finish_task(
        self,
        task_id: str,
        *,
        now: str,
        status: str,
        result_json: Optional[str] = None,
        error_json: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> None:
        """标记任务终态。trade_date 非空时同时校正业务日期。

        为什么要校正:抢占时只能用本地已确认交易日预解析 trade_date,
        在线模式下 Tushare 可能返回更新的交易日,实际写入的 as_of 会晚于
        抢占时的键。若不回写,下一次同 as_of 的重跑会因为键不匹配而放行,
        幂等失效。回写后键与真实批次对齐,重复运行能被正确拦住。
        """
        if status not in ("succeeded", "failed"):
            raise ValueError(f"非法终态: {status}")
        self.con.execute("BEGIN TRANSACTION")
        try:
            current = self.con.execute(
                "SELECT status FROM task_runs WHERE task_id = ?", [task_id]
            ).fetchone()
            if current is None:
                raise KeyError(f"任务不存在: {task_id}")
            if current[0] in ("succeeded", "failed"):
                if current[0] == status:
                    self.con.execute("COMMIT")
                    return
                raise ValueError(f"任务已是终态，不可改写: {task_id}/{current[0]}")
            if trade_date is not None:
                self._rebind_task_claim_in_transaction(task_id, trade_date)
            self.con.execute(
                "UPDATE task_runs SET status=?, finished_at=?, heartbeat_at=?, "
                "result_json=?, error_json=?, trade_date=COALESCE(?, trade_date) "
                "WHERE task_id = ?",
                [status, now, now, result_json, error_json, trade_date, task_id],
            )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self.con.execute(
            "SELECT task_id, kind, trade_date, strategy, status, created_at, "
            "started_at, finished_at, heartbeat_at, result_json, error_json "
            "FROM task_runs WHERE task_id = ?",
            [task_id],
        ).fetchone()
        if row is None:
            return None
        keys = ("task_id", "kind", "trade_date", "strategy", "status", "created_at",
                "started_at", "finished_at", "heartbeat_at", "result_json", "error_json")
        return dict(zip(keys, row))

    def recent_tasks(self, limit: int = 50, kind: Optional[str] = None) -> pd.DataFrame:
        if kind:
            return self.con.execute(
                "SELECT * FROM task_runs WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                [kind, limit],
            ).df()
        return self.con.execute(
            "SELECT * FROM task_runs ORDER BY created_at DESC LIMIT ?", [limit]
        ).df()

    def all_picks(self, strategy: Optional[str] = None) -> pd.DataFrame:
        """读取全部选股台账(供 IC / 胜率 / 分层统计)。"""
        if strategy:
            return self.con.execute(
                "SELECT * FROM picks WHERE strategy = ? ORDER BY run_date, rank",
                [strategy],
            ).df()
        return self.con.execute("SELECT * FROM picks ORDER BY run_date, rank").df()

