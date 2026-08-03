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

_PICKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    run_date     VARCHAR,
    as_of        VARCHAR,
    strategy     VARCHAR,
    ts_code      VARCHAR,
    name         VARCHAR,
    industry     VARCHAR,
    rank         INTEGER,
    total        DOUBLE,
    money_class  VARCHAR,
    one_line     VARCHAR,
    contrib_json VARCHAR,
    feat_json    VARCHAR,
    ret1         DOUBLE,
    ret3         DOUBLE,
    ret5         DOUBLE,
    ret10        DOUBLE,
    PRIMARY KEY (as_of, strategy, ts_code)
);
"""

_SCHEMA = f"""
{_PICKS_SCHEMA}
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code   VARCHAR PRIMARY KEY,
    symbol    VARCHAR,
    name      VARCHAR,
    area      VARCHAR,
    industry  VARCHAR,
    market    VARCHAR,
    list_date VARCHAR
);
CREATE TABLE IF NOT EXISTS daily (
    ts_code    VARCHAR,
    trade_date VARCHAR,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    pre_close  DOUBLE,
    pct_chg    DOUBLE,
    vol        DOUBLE,
    amount     DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code       VARCHAR,
    trade_date    VARCHAR,
    turnover_rate DOUBLE,
    volume_ratio  DOUBLE,
    total_mv      DOUBLE,
    circ_mv       DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS moneyflow (
    ts_code         VARCHAR,
    trade_date      VARCHAR,
    net_mf_amount   DOUBLE,
    buy_lg_amount   DOUBLE,
    sell_lg_amount  DOUBLE,
    buy_elg_amount  DOUBLE,
    sell_elg_amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS trade_cal (
    exchange   VARCHAR,
    cal_date   VARCHAR,
    is_open    INTEGER,
    PRIMARY KEY (exchange, cal_date)
);
{_PICKS_SCHEMA}
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id                VARCHAR PRIMARY KEY,
    run_date              VARCHAR,
    as_of                 VARCHAR,
    strategy              VARCHAR,
    candidate_count       INTEGER,
    scored_count          INTEGER,
    passed_count          INTEGER,
    final_count           INTEGER,
    top_industries_json   VARCHAR
);
CREATE TABLE IF NOT EXISTS scan_rows (
    run_id               VARCHAR,
    ts_code              VARCHAR,
    name                 VARCHAR,
    industry             VARCHAR,
    rank                 INTEGER,
    total                DOUBLE,
    passed               BOOLEAN,
    selected             BOOLEAN,
    gate_reasons_json    VARCHAR,
    cat_scores_json      VARCHAR,
    money_class          VARCHAR,
    one_line             VARCHAR,
    contrib_json         VARCHAR,
    feat_json            VARCHAR,
    PRIMARY KEY (run_id, ts_code)
);
CREATE TABLE IF NOT EXISTS task_runs (
    task_id      VARCHAR PRIMARY KEY,
    kind         VARCHAR,   -- scan / postmortem / news / close_pipeline
    trade_date   VARCHAR,   -- 业务日期(as_of),幂等作用域的一部分
    strategy     VARCHAR,
    status       VARCHAR,   -- queued / running / succeeded / failed
    created_at   VARCHAR,   -- ISO8601(带时区)
    started_at   VARCHAR,
    finished_at  VARCHAR,
    heartbeat_at VARCHAR,   -- 运行中心跳,用于识别进程崩溃遗留的僵死任务
    result_json  VARCHAR,
    error_json   VARCHAR
);
CREATE TABLE IF NOT EXISTS news_sources (
    source_id    VARCHAR PRIMARY KEY,  -- 采集器内部标识,如 cninfo_notice
    name         VARCHAR,              -- 展示名
    kind         VARCHAR,              -- notice(公告) / news(新闻) / research(研报)
    home_url     VARCHAR,
    -- 来源基准可信度(0~1)。交易所/证监会公告高于门户转载。
    -- 它只是"来源"这一维,单条舆情的最终可信度还要结合正文完整性等因素。
    base_credibility DOUBLE,
    -- 合规备注:该来源的 robots / 使用条款结论,便于事后审计"为什么可以采"
    compliance_note  VARCHAR,
    enabled      BOOLEAN
);
CREATE TABLE IF NOT EXISTS news_items (
    news_id      VARCHAR PRIMARY KEY,  -- 规范化链接的哈希,天然幂等
    source_id    VARCHAR,
    title        VARCHAR,              -- 原文标题,不改写
    summary      VARCHAR,              -- 原文摘要/首段;来源没有就留 NULL,不自造
    url          VARCHAR,              -- 原始链接,研判可追溯的落点
    published_at VARCHAR,              -- 来源给出的发布时间(ISO8601)
    fetched_at   VARCHAR,              -- 本地抓取时间(ISO8601),与发布时间分开存
    trade_date   VARCHAR,              -- 归属交易日(按发布时间与收盘时点映射)
    -- 去重指纹:标题规范化后的哈希。同一条新闻被多家转载时 url 不同,
    -- 靠它归并;保留各自原始行,不删数据,只标 duplicate_of。
    dedup_key    VARCHAR,
    duplicate_of VARCHAR,              -- 指向首次出现的 news_id;自身为首条则 NULL
    event_type   VARCHAR,              -- 事件分类,未能判定时为 NULL(显示"未分类")
    sentiment    VARCHAR,              -- positive / negative / neutral;判不出为 NULL
    sentiment_score DOUBLE,            -- -1~1;判不出为 NULL,绝不填 0 冒充中性
    credibility  DOUBLE,               -- 0~1 综合可信度
    raw_json     VARCHAR               -- 来源原始字段,便于回溯与重算
);
CREATE TABLE IF NOT EXISTS news_links (
    news_id      VARCHAR,
    -- 关联对象:股票用 ts_code,行业用行业名。两者共用一张表,靠 link_type 区分
    link_type    VARCHAR,              -- stock / industry
    link_key     VARCHAR,
    -- 关联依据:code_in_text / name_in_text / source_field 等。
    -- 必填。没有依据的关联不写入——"这条新闻和这只股票有关"必须能说出为什么。
    match_basis  VARCHAR,
    match_text   VARCHAR,              -- 命中的原文片段,供页面展示佐证
    confidence   DOUBLE,               -- 0~1 关联置信度
    PRIMARY KEY (news_id, link_type, link_key)
);

-- 自选股:用户手动维护的观察池,与选股台账互相独立
CREATE TABLE IF NOT EXISTS watchlist (
    ts_code    VARCHAR PRIMARY KEY,
    name       VARCHAR,
    note       VARCHAR,                -- 用户备注,可空
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at   VARCHAR                 -- ISO8601
);
-- 机器学习训练记录:一次训练一个 run_id,指标与特征重要性落 JSON
CREATE TABLE IF NOT EXISTS ml_runs (
    run_id        VARCHAR PRIMARY KEY,
    model         VARCHAR,             -- 模型标识,如 gbdt / rf
    horizon_days  INTEGER,             -- 预测周期(T+N)
    trained_at    VARCHAR,             -- ISO8601
    train_start   VARCHAR,
    train_end     VARCHAR,
    val_start     VARCHAR,
    val_end       VARCHAR,
    n_train       INTEGER,
    n_val         INTEGER,
    n_features    INTEGER,
    features_json VARCHAR,             -- [{{name, importance}}]
    metrics_json  VARCHAR,             -- {{accuracy, auc, ic, ...}}
    notes         VARCHAR
);
-- 机器学习预测打分:最新截面每只股票一个 score/prob_up
CREATE TABLE IF NOT EXISTS ml_predictions (
    run_id     VARCHAR,
    ts_code    VARCHAR,
    trade_date VARCHAR,                -- 预测截面(as_of)
    score      DOUBLE,                 -- 模型输出(概率或归一化分)
    prob_up    DOUBLE,                 -- 预测上涨概率 0~1
    PRIMARY KEY (run_id, ts_code, trade_date)
);
-- 回测运行记录:策略/基准的净值曲线与统计指标
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       VARCHAR PRIMARY KEY,
    kind         VARCHAR,              -- strategy / benchmark
    strategy     VARCHAR,              -- 策略名,基准行为空
    as_of_start  VARCHAR,
    as_of_end    VARCHAR,
    n_points     INTEGER,              -- 净值曲线点数
    metrics_json VARCHAR,              -- {{total_return, max_drawdown, win_rate, ic, sharpe,...}}
    equity_json  VARCHAR,              -- [{{date, nav}}]
    created_at   VARCHAR               -- ISO8601
);

-- 多 agent 短线研判:每次研判一个批次(粗筛/深度学习/辩论/最终)
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id        VARCHAR PRIMARY KEY,
    as_of         VARCHAR,              -- 行情截面(最新交易日)
    status        VARCHAR,              -- queued / running / succeeded / failed
    stage         VARCHAR,              -- coarse / deep / debate / done
    candidates    INTEGER,              -- 粗筛候选数量(请求值)
    depth         INTEGER,              -- 深度学习数量(请求值)
    final_count   INTEGER,              -- 最终输出数量(请求值)
    progress_json VARCHAR,              -- {{stage, step, total, message, at}}
    created_at    VARCHAR,              -- ISO8601(带时区)
    started_at    VARCHAR,
    finished_at   VARCHAR,
    heartbeat_at  VARCHAR,
    error_json    VARCHAR,
    result_json   VARCHAR               -- 批次摘要({{run_id, as_of, final: [...]}})
);
-- 多 agent 研判结果:每个 run 内每只入选股票一行
CREATE TABLE IF NOT EXISTS agent_judgments (
    run_id     VARCHAR,
    ts_code    VARCHAR,
    name       VARCHAR,
    industry   VARCHAR,
    rank       INTEGER,
    score      DOUBLE,                  -- 综合得分 0~100
    stance     VARCHAR,                 -- bullish / neutral / bearish
    thesis     VARCHAR,                 -- 核心逻辑(自然语言)
    risks      VARCHAR,                 -- JSON 数组:风险点
    stage_json VARCHAR,                 -- JSON:粗筛理由/三分析师/辩论纪要
    PRIMARY KEY (run_id, ts_code)
);
"""


class Store:
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
            self._migrate_picks_pk()

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
        self.con.register("_stg", d)
        key_list = [k for k in keys if k in use]
        if key_list:
            on = " AND ".join(f"t.{k} = s.{k}" for k in key_list)
            self.con.execute(
                f"DELETE FROM {table} t USING _stg s WHERE {on}"
            )
        collist = ", ".join(use)
        self.con.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _stg")
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
    ) -> None:
        """加入自选股:已存在保持原顺序(幂等),新加入追加到末尾。"""
        existed = self.con.execute(
            "SELECT 1 FROM watchlist WHERE ts_code = ?", [ts_code]
        ).fetchone()
        if existed is not None:
            return
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
        run_df = pd.DataFrame([run_row])
        as_of = run_row.get("as_of")
        strategy = run_row.get("strategy")
        self.con.execute("BEGIN TRANSACTION")
        try:
            if as_of is not None and strategy is not None:
                # 先按业务键找出待清理的旧 run_id(排除本次),再删明细与批次
                stale = self.con.execute(
                    "SELECT run_id FROM scan_runs "
                    "WHERE as_of = ? AND strategy = ? AND run_id <> ?",
                    [as_of, strategy, run_row.get("run_id")],
                ).fetchall()
                for (old_run_id,) in stale:
                    self.con.execute(
                        "DELETE FROM scan_rows WHERE run_id = ?", [old_run_id]
                    )
                    self.con.execute(
                        "DELETE FROM scan_runs WHERE run_id = ?", [old_run_id]
                    )
            self.upsert("scan_runs", run_df, keys=("run_id",))
            self.upsert("scan_rows", rows, keys=("run_id", "ts_code"))
            self.con.execute("COMMIT")
        except Exception:
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
        """尝试登记一个任务,返回 (是否抢到, 冲突任务或 None)。

        幂等作用域 = (kind, trade_date, strategy)。语义:
        - 已有 succeeded 记录  -> 不抢(除非 force),让调用方跳过。
          这就是"同一交易日重复运行不产生重复复盘"的落点。
        - 已有 queued/running 且心跳未超时 -> 不抢,防跨进程并发重复写。
        - 已有 queued/running 但心跳超时(进程崩溃遗留) -> 判为僵死,允许抢占。
        - 已有 failed -> 允许重试。

        写入放在单事务内。DuckDB 单写者模型下,跨进程的并发抢占由文件写锁
        串行化,故"检查+插入"不会两个进程同时成功。
        """
        self.con.execute("BEGIN TRANSACTION")
        try:
            existing = self.con.execute(
                """
                SELECT task_id, status, created_at, started_at, finished_at,
                       heartbeat_at, result_json, error_json
                FROM task_runs
                WHERE kind = ? AND trade_date = ?
                  AND COALESCE(strategy, '') = COALESCE(?, '')
                ORDER BY created_at DESC LIMIT 1
                """,
                [kind, trade_date, strategy],
            ).fetchone()

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
                if status == "succeeded":
                    self.con.execute("COMMIT")
                    return False, conflict
                if status in ("queued", "running"):
                    if not _is_stale(existing[5] or existing[2], now, stale_after_seconds):
                        self.con.execute("COMMIT")
                        return False, conflict
                    # 心跳超时:标记僵死,继续往下抢占
                    self.con.execute(
                        """
                        UPDATE task_runs
                        SET status = 'failed',
                            finished_at = ?,
                            error_json = ?
                        WHERE task_id = ?
                        """,
                        [
                            now,
                            '{"type":"StaleTask","message":"心跳超时,判定进程已崩溃"}',
                            existing[0],
                        ],
                    )

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
            return True, None
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def mark_task_running(self, task_id: str, now: str) -> None:
        self.con.execute(
            "UPDATE task_runs SET status='running', started_at=?, heartbeat_at=? "
            "WHERE task_id = ?",
            [now, now, task_id],
        )

    def task_heartbeat(self, task_id: str, now: str) -> None:
        self.con.execute(
            "UPDATE task_runs SET heartbeat_at = ? WHERE task_id = ?", [now, task_id]
        )

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
        if trade_date is None:
            self.con.execute(
                "UPDATE task_runs SET status=?, finished_at=?, heartbeat_at=?, "
                "result_json=?, error_json=? WHERE task_id = ?",
                [status, now, now, result_json, error_json, task_id],
            )
            return
        self.con.execute(
            "UPDATE task_runs SET status=?, finished_at=?, heartbeat_at=?, "
            "result_json=?, error_json=?, trade_date=? WHERE task_id = ?",
            [status, now, now, result_json, error_json, trade_date, task_id],
        )

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

    # ------------------------------------------------------------ 舆情
    def upsert_news_sources(self, df: pd.DataFrame) -> int:
        """登记/更新舆情来源。source_id 为键,重复登记不产生重复行。"""
        return self.upsert("news_sources", df, keys=("source_id",))

    def upsert_news_items(self, df: pd.DataFrame) -> int:
        """写入舆情条目。news_id(规范化链接哈希)为键,重复采集天然幂等。"""
        return self.upsert("news_items", df, keys=("news_id",))

    def upsert_news_links(self, df: pd.DataFrame) -> int:
        """写入舆情与股票/行业的关联。"""
        return self.upsert("news_links", df, keys=("news_id", "link_type", "link_key"))

    def existing_news_ids(self, news_ids: Iterable[str]) -> set[str]:
        """返回已入库的 news_id 子集,供采集器跳过重复抓取。"""
        ids = [str(n) for n in news_ids if n]
        if not ids:
            return set()
        frame = pd.DataFrame({"news_id": ids})
        self.con.register("_ids", frame)
        # try/finally:查询抛错时也要摘掉临时视图,否则它会挂在连接上,
        # 下一次 register 同名视图的行为取决于 DuckDB 版本,不该赌。
        try:
            rows = self.con.execute(
                "SELECT n.news_id FROM news_items n JOIN _ids i ON n.news_id = i.news_id"
            ).fetchall()
        finally:
            self.con.unregister("_ids")
        return {r[0] for r in rows}

    def find_dedup_originals(self, dedup_keys: Iterable[str]) -> dict[str, str]:
        """按去重指纹查首条 news_id,返回 {dedup_key: 最早的 news_id}。

        取最早发布的那条作为"原始条目",后来的转载指向它。用 published_at
        而不是抓取时间排序:谁先发布是事实,谁先被我们抓到只是采集顺序。
        """
        keys = [str(k) for k in dedup_keys if k]
        if not keys:
            return {}
        frame = pd.DataFrame({"dedup_key": keys})
        self.con.register("_keys", frame)
        try:
            rows = self.con.execute(
                """
                SELECT n.dedup_key, n.news_id FROM news_items n
                JOIN _keys k ON n.dedup_key = k.dedup_key
                WHERE n.duplicate_of IS NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY n.dedup_key ORDER BY n.published_at ASC, n.news_id ASC
                ) = 1
                """
            ).fetchall()
        finally:
            self.con.unregister("_keys")
        return {r[0]: r[1] for r in rows}

    def news_by_trade_date(
        self,
        trade_date: str,
        *,
        include_duplicates: bool = False,
        limit: int = 200,
    ) -> pd.DataFrame:
        """某交易日的舆情条目(带来源名与基准可信度)。

        默认剔除转载副本:统计"今日重要舆情"时,同一条新闻被五家转载
        不该变成五条证据。需要看全量时传 include_duplicates=True。
        """
        dup_filter = "" if include_duplicates else "AND n.duplicate_of IS NULL"
        return self.con.execute(
            f"""
            SELECT n.*, s.name AS source_name, s.kind AS source_kind,
                   s.base_credibility, s.home_url AS source_home_url
            FROM news_items n
            LEFT JOIN news_sources s ON n.source_id = s.source_id
            WHERE n.trade_date = ? {dup_filter}
            ORDER BY n.credibility DESC NULLS LAST, n.published_at DESC
            LIMIT ?
            """,
            [trade_date, limit],
        ).df()

    def news_for_link(
        self,
        *,
        link_type: str,
        link_key: str,
        as_of: Optional[str] = None,
        trade_date: Optional[str] = None,
        limit: int = 50,
    ) -> pd.DataFrame:
        """某只股票/某个行业相关的舆情,按发布时间倒序。

        as_of 非空时只返回 <= as_of 的条目。这条过滤是前视纪律的一部分:
        复盘 T 日时读到 T+1 的新闻,会让"舆情解释了走势"变成事后诸葛。
        trade_date 非空时只看指定交易日(舆情页按板块下钻用)。
        """
        if link_type not in ("stock", "industry"):
            raise ValueError(f"非法关联类型: {link_type}")
        params: list = [link_type, link_key]
        date_filter = ""
        if trade_date:
            date_filter = "AND n.trade_date = ?"
            params.append(trade_date)
        if as_of:
            date_filter += " AND n.trade_date <= ?"
            params.append(as_of)
        params.append(limit)
        return self.con.execute(
            f"""
            SELECT n.*, l.match_basis, l.match_text, l.confidence AS link_confidence,
                   s.name AS source_name, s.kind AS source_kind, s.base_credibility,
                   s.home_url AS source_home_url
            FROM news_links l
            JOIN news_items n ON l.news_id = n.news_id
            LEFT JOIN news_sources s ON n.source_id = s.source_id
            WHERE l.link_type = ? AND l.link_key = ? {date_filter}
              AND n.duplicate_of IS NULL
            ORDER BY n.published_at DESC
            LIMIT ?
            """,
            params,
        ).df()

    def news_industry_summary(
        self,
        trade_date: str,
        *,
        limit: int = 50,
    ) -> pd.DataFrame:
        """某交易日按行业板块聚合的舆情统计(剔重后)。

        一个板块下同一条新闻只计一次(COUNT DISTINCT);同一条新闻命中多个
        板块时各计各的,页面据此如实展示"这个板块今天有几条相关舆情"。
        情绪按相同口径逐项计数:sentiment 为空或不在三态里的一律算"未判定",
        不拿中性冒充。
        """
        return self.con.execute(
            """
            SELECT l.link_key AS industry,
                   COUNT(DISTINCT n.news_id) AS news_count,
                   COUNT(DISTINCT CASE WHEN n.sentiment = 'positive' THEN n.news_id END) AS positive,
                   COUNT(DISTINCT CASE WHEN n.sentiment = 'negative' THEN n.news_id END) AS negative,
                   COUNT(DISTINCT CASE WHEN n.sentiment = 'neutral' THEN n.news_id END) AS neutral,
                   COUNT(DISTINCT CASE WHEN n.sentiment IS NULL
                                        OR n.sentiment NOT IN ('positive','negative','neutral')
                                   THEN n.news_id END) AS undecided
            FROM news_links l
            JOIN news_items n ON l.news_id = n.news_id
            WHERE l.link_type = 'industry'
              AND n.trade_date = ?
              AND n.duplicate_of IS NULL
            GROUP BY l.link_key
            ORDER BY news_count DESC, l.link_key
            LIMIT ?
            """,
            [trade_date, limit],
        ).df()

    def news_unlinked_industry_count(self, trade_date: str) -> int:
        """某交易日没有任何行业关联的舆情条数(剔重后)。

        页面据此如实显示"还有 N 条没匹配到行业",而不是把新闻硬塞进某个
        板块。注意它与行业聚合总数不相等:一条新闻命中多个板块会被每个板块
        各计一次,而这里只数"一条都没命中"的。
        """
        row = self.con.execute(
            """
            SELECT COUNT(DISTINCT n.news_id)
            FROM news_items n
            WHERE n.trade_date = ?
              AND n.duplicate_of IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM news_links l
                  WHERE l.news_id = n.news_id AND l.link_type = 'industry'
              )
            """,
            [trade_date],
        ).fetchone()
        return int(row[0]) if row else 0

    def news_sources(self) -> pd.DataFrame:
        """全部已登记来源。供页面展示"数据来自哪里"与合规审计。"""
        return self.con.execute(
            "SELECT * FROM news_sources ORDER BY source_id"
        ).df()


    # ------------------------------------------------------------ 多 agent 研判
    def record_agent_run(self, run_row: dict) -> None:
        """插入一条研判批次。run_id 为键,重复提交会报主键冲突。"""
        self.con.execute(
            """
            INSERT INTO agent_runs (
                run_id, as_of, status, stage, candidates, depth, final_count,
                progress_json, created_at, started_at, finished_at, heartbeat_at,
                error_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_row["run_id"], run_row.get("as_of"), run_row.get("status"),
                run_row.get("stage"), run_row.get("candidates"),
                run_row.get("depth"), run_row.get("final_count"),
                run_row.get("progress_json"), run_row.get("created_at"),
                run_row.get("started_at"), run_row.get("finished_at"),
                run_row.get("heartbeat_at"), run_row.get("error_json"),
                run_row.get("result_json"),
            ],
        )

    def update_agent_run(self, run_id: str, **fields) -> None:
        """按 run_id 更新指定列。只更新调用方显式传的字段。"""
        if not fields:
            return
        allowed = {
            "as_of", "status", "stage", "candidates", "depth", "final_count",
            "progress_json", "started_at", "finished_at", "heartbeat_at",
            "error_json", "result_json",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"agent_runs 无这些列: {sorted(unknown)}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        self.con.execute(
            f"UPDATE agent_runs SET {assignments} WHERE run_id = ?",
            [*fields.values(), run_id],
        )

    def get_agent_run(self, run_id: str) -> Optional[dict]:
        """按 run_id 取研判批次行;不存在返回 None。"""
        row = self.con.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in self.con.description], row))

    def recent_agent_runs(
        self, limit: int = 20, as_of: Optional[str] = None, status: Optional[str] = None
    ) -> pd.DataFrame:
        """最近研判批次,按创建时间倒序;as_of/status 给定时先过滤再限量。"""
        clauses: list[str] = []
        params: list[object] = []
        if as_of:
            clauses.append("as_of = ?")
            params.append(as_of)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self.con.execute(
            f"SELECT * FROM agent_runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).df()

    def upsert_agent_judgments(self, df: pd.DataFrame) -> int:
        """写入研判结果行,run_id+ts_code 为键,重复写入幂等。"""
        return self.upsert("agent_judgments", df, keys=("run_id", "ts_code"))

    def agent_judgments(self, run_id: str) -> pd.DataFrame:
        """某批次全部研判结果,按排名升序。"""
        return self.con.execute(
            "SELECT * FROM agent_judgments WHERE run_id = ? ORDER BY rank, ts_code",
            [run_id],
        ).df()

    def news_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """已入库舆情覆盖的交易日区间。两端为空表示还没采过。"""
        row = self.con.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM news_items"
        ).fetchone()
        if not row:
            return None, None
        return row[0], row[1]

