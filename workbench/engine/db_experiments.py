"""实验批次与四组明细的 DuckDB 存储方法。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from collections.abc import Iterable, Mapping
from numbers import Integral, Real
from typing import Any

import pandas as pd


_RUN_COLUMNS = (
    "run_id",
    "as_of",
    "data_cutoff_at",
    "status",
    "strategy_name",
    "strategy_version",
    "model",
    "temperature",
    "prompt_version",
    "candidate_hash",
    "candidate_count",
    "final_count",
    "hybrid_rule_weight",
    "hybrid_ai_weight",
    "created_at",
    "finished_at",
    "error_json",
)

_DECISION_COLUMNS = (
    "run_id",
    "group_name",
    "ts_code",
    "name",
    "industry",
    "rank",
    "rule_score",
    "ai_score",
    "hybrid_score",
    "reason_json",
    "risk_json",
)

_REQUIRED_GROUPS = frozenset({"rule", "ai", "hybrid", "benchmark"})
_CREATABLE_STATUSES = frozenset({"queued", "running"})
_REQUIRED_RUN_TEXT_FIELDS = (
    "run_id",
    "as_of",
    "data_cutoff_at",
    "strategy_name",
    "strategy_version",
    "prompt_version",
    "candidate_hash",
)
_AUDIT_RUN_COLUMNS = (
    "as_of",
    "data_cutoff_at",
    "strategy_name",
    "strategy_version",
    "model",
    "temperature",
    "prompt_version",
    "candidate_hash",
    "candidate_count",
    "final_count",
    "hybrid_rule_weight",
    "hybrid_ai_weight",
)
_DECISIONS_STAGE = "_experiment_decisions_stage"

# 成交状态:同一条决策的 10 个 horizon 共享同一次成交,因此行级判断即可。
#   filled            -- 买到了,entry_price 有值
#   entry_unavailable -- 买不到,涨停封板。这是市场事实,终局。
#   entry_bar_missing -- 买入日没有 K 线。**不是终局**:本项目每轮扫描只回补
#                        当轮候选池的日线(见 run_scan._backfill_history),所以
#                        更早批次的票在后续买入日经常整片缺行。补上行情就能算出
#                        成交,把它当终局会让这些样本永久失去测量机会。
#   pending_entry     -- 还没定,通常是缺涨跌停价或行情没到
_ENTRY_UNAVAILABLE_STATUSES = ("entry_unavailable",)
ENTRY_STATUSES = ("filled", "entry_unavailable", "pending_entry")


def _shift_iso(moment: str, seconds: float) -> str:
    """把 ISO 时刻平移若干秒，仍返回 ISO 字符串。

    库里存的是 ISO 文本，字符串比较对同一时区的等长格式是正确的排序；
    这里只负责算出比较基准，不改变存储格式。
    """
    parsed = datetime.fromisoformat(moment)
    return (parsed + timedelta(seconds=seconds)).isoformat()


def entry_status_predicate(entry_status: str, *, alias: str) -> str:
    """把成交状态翻成 experiment_returns 上的行级 SQL 条件。"""
    if entry_status not in ENTRY_STATUSES:
        raise ValueError(f"非法成交状态: {entry_status}")
    unavailable = ", ".join(f"'{status}'" for status in _ENTRY_UNAVAILABLE_STATUSES)
    if entry_status == "filled":
        return f"{alias}.entry_price IS NOT NULL"
    if entry_status == "entry_unavailable":
        return f"{alias}.status IN ({unavailable})"
    return (
        f"{alias}.entry_price IS NULL"
        f" AND COALESCE({alias}.status, '') NOT IN ({unavailable})"
    )


def classify_entry_status(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """按收益明细判断一条决策的成交状态,和上面的 SQL 条件同一套规则。

    还没算过收益的决策返回 None——"没算"和"算过但买不到"是两回事,不合并。
    """
    seen = False
    unavailable = False
    for row in rows:
        seen = True
        if row.get("entry_price") is not None:
            return "filled"
        if row.get("status") in _ENTRY_UNAVAILABLE_STATUSES:
            unavailable = True
    if not seen:
        return None
    return "entry_unavailable" if unavailable else "pending_entry"


class ExperimentMixin:
    """只负责实验台账的写入、查询与事务边界。"""

    con: Any

    @staticmethod
    def _validate_run_row(row: Mapping[str, Any]) -> None:
        unknown = set(row) - set(_RUN_COLUMNS)
        if unknown:
            raise ValueError(f"非法实验批次字段: {sorted(unknown)}")
        for field in _REQUIRED_RUN_TEXT_FIELDS:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"实验批次字段不可为空: {field}")
        if row.get("status") not in _CREATABLE_STATUSES:
            raise ValueError("实验批次初始状态只能是 queued 或 running")

        candidate_count = row.get("candidate_count")
        final_count = row.get("final_count")
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, Integral)
            or candidate_count <= 0
        ):
            raise ValueError("candidate_count 必须是正整数")
        if (
            isinstance(final_count, bool)
            or not isinstance(final_count, Integral)
            or final_count <= 0
        ):
            raise ValueError("final_count 必须是正整数")
        if final_count > candidate_count:
            raise ValueError("final_count 不可大于 candidate_count")

        temperature = row.get("temperature")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, Real)
            or not math.isfinite(float(temperature))
        ):
            raise ValueError("temperature 必须是有限数值")

        rule_weight = row.get("hybrid_rule_weight")
        ai_weight = row.get("hybrid_ai_weight")
        if any(
            isinstance(weight, bool)
            or not isinstance(weight, Real)
            or not math.isfinite(float(weight))
            or not 0 <= weight <= 1
            for weight in (rule_weight, ai_weight)
        ):
            raise ValueError("混合权重必须在 0 到 1 之间")
        if not math.isclose(
            float(rule_weight) + float(ai_weight), 1.0, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("混合权重之和必须为 1")

    def _insert_experiment_run(self, row: Mapping[str, Any]) -> None:
        columns = ", ".join(_RUN_COLUMNS)
        placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
        self.con.execute(
            f"INSERT INTO experiment_runs ({columns}) VALUES ({placeholders})",
            [row.get(column) for column in _RUN_COLUMNS],
        )

    def create_experiment_run(self, row: Mapping[str, Any]) -> bool:
        """创建 queued/running 批次；已有批次保持原样。"""
        self._validate_run_row(row)
        columns = ", ".join(_RUN_COLUMNS)
        placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
        inserted = self.con.execute(
            f"""
            INSERT INTO experiment_runs ({columns}) VALUES ({placeholders})
            ON CONFLICT DO NOTHING
            RETURNING run_id
            """,
            [row.get(column) for column in _RUN_COLUMNS],
        ).fetchone()
        return inserted is not None

    def fail_experiment_run(
        self, run_id: str, finished_at: str, error_json: str | None
    ) -> None:
        """把未完成批次标记为失败，成功批次不可降级。"""
        updated = self.con.execute(
            """
            UPDATE experiment_runs
            SET status = 'failed', finished_at = ?, error_json = ?
            WHERE run_id = ? AND status IN ('queued', 'running', 'failed')
            RETURNING run_id
            """,
            [finished_at, error_json, run_id],
        ).fetchone()
        if updated is not None:
            return

        current = self.con.execute(
            "SELECT status FROM experiment_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if current is None:
            raise KeyError(f"实验批次不存在: {run_id}")
        if current[0] == "succeeded":
            raise ValueError(f"成功实验批次不可标记失败: {run_id}")
        raise ValueError(f"实验批次状态不可标记失败: {run_id}/{current[0]}")

    def reclaim_stale_experiment_runs(
        self, *, now: str, max_idle_seconds: float = 7200.0
    ) -> list[str]:
        """把无人照看的 running 批次标记为失败，返回被回收的 run_id。

        `running` 在库里区分不出「正在跑」和「跑它的进程早就死了」。收尾逻辑
        写在流程函数尾部，进程被强杀就永远不执行，批次于是永久停在 running。

        判据是「多久没动静」而不是「是否 running」：同一个库目录可能同时起了
        两个服务实例，无条件回收会把另一个进程正在跑的批次误标成失败。边界取
        `<=` 而不是 `<`，让窗口值本身就是「达到即回收」的语义。
        """
        cutoff = _shift_iso(now, -max_idle_seconds)
        stale = self.con.execute(
            """
            SELECT run_id FROM experiment_runs
            WHERE status = 'running' AND created_at <= ?
            ORDER BY created_at
            """,
            [cutoff],
        ).fetchall()
        error_json = '{"reason": "进程中断，启动时回收未收尾的实验批次"}'
        for (run_id,) in stale:
            self.con.execute(
                """
                UPDATE experiment_runs
                SET status = 'failed', finished_at = ?, error_json = ?
                WHERE run_id = ? AND status = 'running'
                """,
                [now, error_json, run_id],
            )
        return [run_id for (run_id,) in stale]

    def reclaim_stale_agent_runs(
        self, *, now: str, max_idle_seconds: float = 7200.0
    ) -> list[str]:
        """同上，但 agent_runs 有 heartbeat_at，按最后一次报活判定。"""
        cutoff = _shift_iso(now, -max_idle_seconds)
        stale = self.con.execute(
            """
            SELECT run_id FROM agent_runs
            WHERE status IN ('queued', 'running')
              AND COALESCE(heartbeat_at, created_at) <= ?
            ORDER BY created_at
            """,
            [cutoff],
        ).fetchall()
        error_json = '{"reason": "进程中断，启动时回收未收尾的 Agent 运行"}'
        for (run_id,) in stale:
            self.con.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', finished_at = ?, error_json = ?
                WHERE run_id = ? AND status IN ('queued', 'running')
                """,
                [now, error_json, run_id],
            )
        return [run_id for (run_id,) in stale]

    def reclaim_stale_task_runs(
        self, *, now: str, max_idle_seconds: float = 7200.0
    ) -> list[str]:
        """同上，作用于 task_runs——前端看板和 /api/pipelines/{id} 读的就是这张表。

        `claim_task` 只是在抢占时把僵死行绕开，行本身仍留着 running，于是任务
        中心永远显示「正在跑」，轮询也永远等不到终态。回收必须连它一起做。
        """
        cutoff = _shift_iso(now, -max_idle_seconds)
        stale = self.con.execute(
            """
            SELECT task_id FROM task_runs
            WHERE status IN ('queued', 'running')
              AND COALESCE(heartbeat_at, started_at, created_at) <= ?
            ORDER BY created_at
            """,
            [cutoff],
        ).fetchall()
        error_json = '{"reason": "进程中断，启动时回收未收尾的任务"}'
        for (task_id,) in stale:
            self.con.execute(
                """
                UPDATE task_runs
                SET status = 'failed', finished_at = ?, error_json = ?
                WHERE task_id = ? AND status IN ('queued', 'running')
                """,
                [now, error_json, task_id],
            )
        return [task_id for (task_id,) in stale]

    def record_failed_experiment_attempt(
        self,
        *,
        run_id: str,
        as_of: str | None,
        strategy_name: str,
        created_at: str,
        finished_at: str,
        error_json: str | None,
    ) -> None:
        """记录尚未形成候选池就失败的尝试，不伪造实验元数据。"""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 不可为空")
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy_name 不可为空")
        current = self.experiment_run(run_id)
        if current is not None:
            self.fail_experiment_run(run_id, finished_at, error_json)
            return
        self.con.execute(
            """
            INSERT INTO experiment_runs (
                run_id, as_of, status, strategy_name,
                created_at, finished_at, error_json
            ) VALUES (?, ?, 'failed', ?, ?, ?, ?)
            """,
            [run_id, as_of, strategy_name, created_at, finished_at, error_json],
        )

    @staticmethod
    def _validated_decisions(
        run_row: Mapping[str, Any], decisions: pd.DataFrame
    ) -> pd.DataFrame:
        run_id = str(run_row["run_id"])
        if decisions is None or decisions.empty:
            raise ValueError("实验明细至少要包含一个可用实验组")
        unknown = set(decisions.columns) - set(_DECISION_COLUMNS)
        if unknown:
            raise ValueError(f"非法实验明细字段: {sorted(unknown)}")
        required = {"run_id", "group_name", "ts_code"}
        missing = required - set(decisions.columns)
        if missing:
            raise ValueError(f"实验明细缺少字段: {sorted(missing)}")
        if decisions[list(required)].isna().any().any():
            raise ValueError("实验明细的 run_id、group_name、ts_code 不可为空")

        run_ids = set(decisions["run_id"].tolist())
        if run_ids != {run_id}:
            raise ValueError("实验明细混入其他 run_id")
        groups = set(decisions["group_name"].tolist())
        unknown_groups = groups - _REQUIRED_GROUPS
        if unknown_groups:
            raise ValueError(f"实验明细包含未知组: {sorted(unknown_groups)}")
        if decisions.duplicated(["group_name", "ts_code"]).any():
            raise ValueError("实验组内包含重复 ts_code")

        counts = decisions.groupby("group_name", dropna=False).size().to_dict()
        maximum_counts = {
            "rule": run_row["final_count"],
            "ai": run_row["final_count"],
            "hybrid": run_row["final_count"],
            "benchmark": run_row["candidate_count"],
        }
        mismatches = [
            f"{group_name}={count}/{maximum_counts[group_name]}"
            for group_name, count in counts.items()
            if count < 1 or count > maximum_counts[group_name]
        ]
        if mismatches:
            raise ValueError(f"实验明细组行数不符合批次元数据: {', '.join(mismatches)}")
        return decisions.reindex(columns=_DECISION_COLUMNS).copy()

    def _has_active_transaction(self) -> bool:
        """用 DuckDB 事务号判定调用方是否已开启显式事务。"""
        # 自动提交模式下每条 SELECT 都有新事务号；显式事务内则复用同一事务号。
        first = self.con.execute("SELECT current_transaction_id()").fetchone()[0]
        second = self.con.execute("SELECT current_transaction_id()").fetchone()[0]
        return first == second

    @staticmethod
    def _validate_existing_audit_metadata(
        run_row: Mapping[str, Any], stored_values: tuple[Any, ...]
    ) -> None:
        mismatches = [
            column
            for column, stored in zip(_AUDIT_RUN_COLUMNS, stored_values)
            if run_row.get(column) != stored
        ]
        if mismatches:
            raise ValueError(f"实验批次审计元数据不一致: {', '.join(mismatches)}")

    def _finish_task_in_transaction(
        self, run_id: str, completion: Mapping[str, Any]
    ) -> None:
        required = {"task_id", "now", "trade_date", "result_json"}
        missing = required - set(completion)
        if missing:
            raise ValueError(f"任务完成信息缺少字段: {sorted(missing)}")
        task_id = completion["task_id"]
        if task_id != run_id:
            raise ValueError("任务 task_id 必须与实验 run_id 一致")
        for field_name in ("now", "trade_date", "result_json"):
            value = completion[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"任务完成信息 {field_name} 不可为空")

        current = self.con.execute(
            "SELECT status FROM task_runs WHERE task_id = ?", [task_id]
        ).fetchone()
        if current is None:
            raise KeyError(f"任务不存在: {task_id}")
        if current[0] not in ("running", "succeeded"):
            raise ValueError(f"任务状态不可标记成功: {task_id}/{current[0]}")
        self._rebind_task_claim_in_transaction(task_id, completion["trade_date"])
        self.con.execute(
            """
            UPDATE task_runs
            SET status = 'succeeded', finished_at = ?, heartbeat_at = ?,
                result_json = ?, error_json = NULL, trade_date = ?
            WHERE task_id = ?
            """,
            [
                completion["now"],
                completion["now"],
                completion["result_json"],
                completion["trade_date"],
                task_id,
            ],
        )

    def record_experiment(
        self,
        run_row: Mapping[str, Any],
        decisions: pd.DataFrame,
        *,
        task_completion: Mapping[str, Any] | None = None,
        scan_completion: Mapping[str, Any] | None = None,
    ) -> None:
        """原子写入扫描、选股、四组实验和任务成功状态。

        一次运行落进 5 张表，其中**两种保存语义**并存，改这里前先看清：

        累积（每次运行独立留存，主键含 run_id）：
        - `experiment_runs`：批次元数据（as_of / created_at / 候选数 / 哈希 / 权重）
        - `experiment_decisions`：四组名单，供台账逐批次追溯
        - `experiment_returns`：成交与各期收益，由后续运行的 backfill 步补

        覆盖（同一信号日只留最后一次，主键不含 run_id）：
        - `picks`：先按 (as_of, strategy) 整组删除再插入。回测与 ML 要的是「每个交易日
          一份不重叠的名单」，同一天留多份会让同一笔钱被重复计入，净值成倍虚高。
        - `scan_runs` / `scan_rows`：同一 (as_of, strategy) 的扫描结果整组替换

        幂等：同一 run_id 已是 succeeded 时直接返回（只补任务终态），不重复写；
        已是 failed 的 run_id 不允许复用——失败批次的元数据不可信，必须换新 run_id。
        """
        self._validate_run_row(run_row)
        run_id = str(run_row["run_id"])
        staged = self._validated_decisions(run_row, decisions)

        if self._has_active_transaction():
            raise RuntimeError("record_experiment 不允许加入调用方的外层事务")

        started = False
        registered = False
        try:
            self.con.execute("BEGIN TRANSACTION")
            started = True
            audit_columns = ", ".join(_AUDIT_RUN_COLUMNS)
            current = self.con.execute(
                f"""
                SELECT status, {audit_columns}
                FROM experiment_runs WHERE run_id = ?
                """,
                [run_id],
            ).fetchone()
            if current is not None:
                self._validate_existing_audit_metadata(run_row, current[1:])
            if current is not None and current[0] == "succeeded":
                if task_completion is not None:
                    self._finish_task_in_transaction(run_id, task_completion)
                self.con.execute("COMMIT")
                started = False
                return
            if current is not None and current[0] == "failed":
                raise ValueError(f"失败实验批次不可复用 run_id: {run_id}")
            if current is None:
                self._insert_experiment_run(run_row)

            if scan_completion is not None:
                required_scan = {"run_row", "rows", "picks", "as_of", "strategy"}
                missing_scan = required_scan - set(scan_completion)
                if missing_scan:
                    raise ValueError(
                        f"扫描完成信息缺少字段: {sorted(missing_scan)}"
                    )
                scan_run = scan_completion["run_row"]
                if scan_run.get("run_id") != run_id:
                    raise ValueError("扫描 run_id 必须与实验 run_id 一致")
                self._record_scan_in_transaction(
                    scan_run, scan_completion["rows"]
                )
                self._replace_picks_in_transaction(
                    str(scan_completion["as_of"]),
                    str(scan_completion["strategy"]),
                    scan_completion["picks"],
                )

            self.con.register(_DECISIONS_STAGE, staged)
            registered = True
            self.con.execute(
                "DELETE FROM experiment_decisions WHERE run_id = ?", [run_id]
            )
            columns = ", ".join(_DECISION_COLUMNS)
            self.con.execute(
                f"""
                INSERT INTO experiment_decisions ({columns})
                SELECT {columns} FROM {_DECISIONS_STAGE}
                """
            )
            self.con.execute(
                """
                UPDATE experiment_runs
                SET status = 'succeeded', finished_at = ?, error_json = ?
                WHERE run_id = ?
                """,
                [run_row.get("finished_at"), run_row.get("error_json"), run_id],
            )
            if task_completion is not None:
                self._finish_task_in_transaction(run_id, task_completion)
            self.con.execute("COMMIT")
            started = False
        except Exception:
            if started:
                self.con.execute("ROLLBACK")
                started = False
            raise
        finally:
            if registered:
                self.con.unregister(_DECISIONS_STAGE)

    def experiment_run(self, run_id: str) -> dict[str, Any] | None:
        """读取单个实验批次。"""
        columns = ", ".join(_RUN_COLUMNS)
        row = self.con.execute(
            f"SELECT {columns} FROM experiment_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if row is None:
            return None
        return dict(zip(_RUN_COLUMNS, row))

    def experiment_decisions(self, run_id: str) -> pd.DataFrame:
        """稳定读取一个批次的四组明细。"""
        columns = ", ".join(_DECISION_COLUMNS)
        return self.con.execute(
            f"""
            SELECT {columns}
            FROM experiment_decisions
            WHERE run_id = ?
            ORDER BY group_name ASC, rank ASC NULLS LAST, ts_code ASC
            """,
            [run_id],
        ).df()

    def experiment_entries_awaiting_limits(self) -> pd.DataFrame:
        """成功批次里成交结果还没定下来的四组明细。

        成交只有一种终局:买到了(``entry_price`` 有值)或买不到(涨停封板)。
        两者都没出现就说明还缺行情或涨跌停价,需要继续补数据。买入日没有 K 线
        (``entry_bar_missing``)不算终局,补上日线后仍要重算,所以这些行会持续
        出现在结果里。``experiment_returns`` 里同一条决策的 10 个 horizon 共享
        同一次成交,所以只要存在任意一行落到终局就算定了。
        """
        unavailable = ", ".join(f"'{status}'" for status in _ENTRY_UNAVAILABLE_STATUSES)
        return self.con.execute(
            f"""
            SELECT d.run_id, r.as_of, d.group_name, d.ts_code
            FROM experiment_decisions d
            JOIN experiment_runs r ON r.run_id = d.run_id
            WHERE r.status = 'succeeded'
              AND NOT EXISTS (
                  SELECT 1 FROM experiment_returns e
                  WHERE e.run_id = d.run_id
                    AND e.group_name = d.group_name
                    AND e.ts_code = d.ts_code
                    AND (
                        e.entry_price IS NOT NULL
                        OR e.status IN ({unavailable})
                    )
              )
            ORDER BY d.run_id ASC, d.group_name ASC, d.rank ASC NULLS LAST,
                     d.ts_code ASC
            """
        ).df()

    def upsert_experiment_returns(self, rows: list[Mapping[str, Any]]) -> int:
        """按 (批次, 组, 股票, horizon) 幂等替换收益明细。"""
        if not rows:
            return 0
        columns = (
            "run_id", "group_name", "ts_code", "horizon", "entry_date",
            "entry_price", "sell_date", "sell_session", "sell_price", "status",
            "reason", "gross_return", "created_at", "updated_at",
        )
        frame = pd.DataFrame([{column: row.get(column) for column in columns} for row in rows])
        self.upsert("experiment_returns", frame, keys=("run_id", "group_name", "ts_code", "horizon"))
        return len(frame)

    def experiment_returns(
        self,
        *,
        run_id: str | None = None,
        group_name: str | None = None,
        ts_code: str | None = None,
        horizon: str | None = None,
        as_of: str | None = None,
        entry_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """按稳定键序读收益明细。

        ``as_of`` 走 experiment_runs 的信号日,``entry_status`` 走成交状态,
        两个筛选和台账页的筛选条件同口径,避免页面上出现第二套算法。
        """
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("group_name", group_name),
            ("ts_code", ts_code),
            ("horizon", horizon),
        ):
            if value is not None:
                conditions.append(f"e.{column} = ?")
                params.append(value)
        if as_of is not None:
            conditions.append(
                "e.run_id IN (SELECT run_id FROM experiment_runs WHERE as_of = ?)"
            )
            params.append(as_of)
        if entry_status is not None:
            conditions.append(entry_status_predicate(entry_status, alias="e"))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = self.con.execute(
            f"SELECT e.* FROM experiment_returns e{where}"
            " ORDER BY e.run_id, e.group_name, e.ts_code, e.horizon",
            params,
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


__all__ = [
    "ExperimentMixin",
    "ENTRY_STATUSES",
    "classify_entry_status",
    "entry_status_predicate",
]
