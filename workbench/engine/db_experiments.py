"""实验批次与四组明细的 DuckDB 存储方法。"""

from __future__ import annotations

from collections.abc import Mapping
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
    "entry_date",
    "entry_price",
    "entry_status",
    "entry_reason",
    "ret1",
    "ret1_target_date",
    "ret1_status",
    "ret1_reason",
    "ret3",
    "ret3_target_date",
    "ret3_status",
    "ret3_reason",
    "ret5",
    "ret5_target_date",
    "ret5_status",
    "ret5_reason",
    "ret10",
    "ret10_target_date",
    "ret10_status",
    "ret10_reason",
)

_REQUIRED_GROUPS = frozenset({"rule", "ai", "hybrid", "benchmark"})
_CREATABLE_STATUSES = frozenset({"queued", "running"})
_UPDATABLE_DECISION_FIELDS = frozenset(
    {
        "entry_date",
        "entry_price",
        "entry_status",
        "entry_reason",
        "ret1",
        "ret1_target_date",
        "ret1_status",
        "ret1_reason",
        "ret3",
        "ret3_target_date",
        "ret3_status",
        "ret3_reason",
        "ret5",
        "ret5_target_date",
        "ret5_status",
        "ret5_reason",
        "ret10",
        "ret10_target_date",
        "ret10_status",
        "ret10_reason",
    }
)


class ExperimentMixin:
    """只负责实验台账的写入、查询与事务边界。"""

    con: Any

    @staticmethod
    def _validate_run_row(row: Mapping[str, Any]) -> None:
        unknown = set(row) - set(_RUN_COLUMNS)
        if unknown:
            raise ValueError(f"非法实验批次字段: {sorted(unknown)}")
        if not row.get("run_id"):
            raise ValueError("实验批次缺少 run_id")
        if row.get("status") not in _CREATABLE_STATUSES:
            raise ValueError("实验批次初始状态只能是 queued 或 running")

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
        exists = self.con.execute(
            "SELECT 1 FROM experiment_runs WHERE run_id = ?", [row["run_id"]]
        ).fetchone()
        if exists is not None:
            return False
        self._insert_experiment_run(row)
        return True

    def fail_experiment_run(
        self, run_id: str, finished_at: str, error_json: str | None
    ) -> None:
        """把未完成批次标记为失败，成功批次不可降级。"""
        current = self.con.execute(
            "SELECT status FROM experiment_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        if current is None:
            raise KeyError(f"实验批次不存在: {run_id}")
        if current[0] == "succeeded":
            raise ValueError(f"成功实验批次不可标记失败: {run_id}")
        self.con.execute(
            """
            UPDATE experiment_runs
            SET status = 'failed', finished_at = ?, error_json = ?
            WHERE run_id = ?
            """,
            [finished_at, error_json, run_id],
        )

    @staticmethod
    def _validated_decisions(run_id: str, decisions: pd.DataFrame) -> pd.DataFrame:
        if decisions is None or decisions.empty:
            raise ValueError("实验明细必须包含 rule、ai、hybrid、benchmark 四组")
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
        if groups != _REQUIRED_GROUPS:
            raise ValueError(
                "实验明细四组必须精确为 rule、ai、hybrid、benchmark"
            )
        if decisions.duplicated(["run_id", "group_name", "ts_code"]).any():
            raise ValueError("实验明细包含重复业务主键")
        return decisions.reindex(columns=_DECISION_COLUMNS).copy()

    def record_experiment(
        self, run_row: Mapping[str, Any], decisions: pd.DataFrame
    ) -> None:
        """在一个事务中写全四组明细，最后才把批次标为成功。"""
        self._validate_run_row(run_row)
        run_id = str(run_row["run_id"])
        staged = self._validated_decisions(run_id, decisions)

        self.con.register("_stg_experiment_decisions", staged)
        self.con.execute("BEGIN TRANSACTION")
        try:
            current = self.con.execute(
                "SELECT status FROM experiment_runs WHERE run_id = ?", [run_id]
            ).fetchone()
            if current is not None and current[0] == "succeeded":
                self.con.execute("COMMIT")
                return
            if current is not None and current[0] == "failed":
                raise ValueError(f"失败实验批次不可复用 run_id: {run_id}")
            if current is None:
                self._insert_experiment_run(run_row)

            self.con.execute(
                "DELETE FROM experiment_decisions WHERE run_id = ?", [run_id]
            )
            columns = ", ".join(_DECISION_COLUMNS)
            self.con.execute(
                f"""
                INSERT INTO experiment_decisions ({columns})
                SELECT {columns} FROM _stg_experiment_decisions
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
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        finally:
            self.con.unregister("_stg_experiment_decisions")

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

    def pending_experiment_decisions(self) -> pd.DataFrame:
        """读取仍需确定成交或回填收益的成功实验明细。"""
        columns = ", ".join(f"d.{column}" for column in _DECISION_COLUMNS)
        return self.con.execute(
            f"""
            SELECT {columns}
            FROM experiment_decisions d
            JOIN experiment_runs r ON r.run_id = d.run_id
            WHERE r.status = 'succeeded'
              AND (
                  d.entry_status IS NULL
                  OR d.entry_status = 'pending_entry'
                  OR (
                      d.entry_status = 'filled'
                      AND (
                          d.ret1_status IS NULL OR d.ret1_status = 'future_not_reached'
                          OR d.ret3_status IS NULL OR d.ret3_status = 'future_not_reached'
                          OR d.ret5_status IS NULL OR d.ret5_status = 'future_not_reached'
                          OR d.ret10_status IS NULL OR d.ret10_status = 'future_not_reached'
                      )
                  )
              )
            ORDER BY d.run_id ASC, d.group_name ASC, d.rank ASC NULLS LAST,
                     d.ts_code ASC
            """
        ).df()

    def update_experiment_decision(
        self,
        run_id: str,
        group_name: str,
        ts_code: str,
        **fields: Any,
    ) -> None:
        """严格按白名单回填成交与收益字段。"""
        if not fields:
            raise ValueError("没有提供实验明细更新字段")
        invalid = set(fields) - _UPDATABLE_DECISION_FIELDS
        if invalid:
            raise ValueError(f"非法实验明细字段: {sorted(invalid)}")
        exists = self.con.execute(
            """
            SELECT 1 FROM experiment_decisions
            WHERE run_id = ? AND group_name = ? AND ts_code = ?
            """,
            [run_id, group_name, ts_code],
        ).fetchone()
        if exists is None:
            raise KeyError(f"实验明细不存在: {run_id}/{group_name}/{ts_code}")

        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.con.execute(
            f"""
            UPDATE experiment_decisions SET {assignments}
            WHERE run_id = ? AND group_name = ? AND ts_code = ?
            """,
            [*fields.values(), run_id, group_name, ts_code],
        )


__all__ = ["ExperimentMixin"]
