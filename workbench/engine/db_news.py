"""`Store` 的舆情与多 agent 研判方法(mixin)。

从 `engine/db.py` 拆出来的,原因是那个文件到了 1182 行,超出项目自定的
800 行上限。拆分口径按**表族**:这里只碰 news_sources / news_items /
news_links / agent_runs / agent_judgments,不碰行情与台账。

为什么用 mixin 而不是独立类:全项目 30 处都写 `from engine.db import Store`
并直接调 `store.news_by_trade_date(...)`。换成组合(`store.news.by_trade_date`)
要改 30 个调用点和一批测试,那是接口变更,不是拆文件。mixin 让调用侧
一行都不用动,`Store` 的方法集合与拆分前完全一致。

纪律与 `db.py` 一致:
- 所有查询按 <= as_of / = trade_date 过滤,不前视。
- 舆情只存来源给出的内容。判不出情绪留 NULL,绝不用 0 冒充"中性"。
"""

from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd


class NewsAgentMixin:
    """舆情 + agent 研判的读写。依赖宿主类提供 `self.con` 与 `self.upsert`。"""

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

