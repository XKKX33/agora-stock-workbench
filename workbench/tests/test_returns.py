from __future__ import annotations

import pandas as pd
import pytest

from engine.db import Store
from engine.returns import HORIZONS, calculate_experiment_returns, returns_summary


def seed_run(store: Store, run_id: str = "run-1", as_of: str = "20260804") -> None:
    run = {
        "run_id": run_id, "as_of": as_of, "data_cutoff_at": as_of,
        "status": "queued", "strategy_name": "test", "strategy_version": "1",
        "model": "test", "temperature": 0.0, "prompt_version": "v1",
        "candidate_hash": "hash", "candidate_count": 1, "final_count": 1,
        "hybrid_rule_weight": 0.5, "hybrid_ai_weight": 0.5,
        "created_at": "2026-08-04T00:00:00+00:00", "finished_at": "2026-08-04T00:00:00+00:00", "error_json": None,
    }
    rows = []
    for group in ("rule", "ai", "hybrid", "benchmark"):
        rows.append({"run_id": run_id, "group_name": group, "ts_code": "000001.SZ", "name": "样本", "industry": "测试", "rank": 1, "rule_score": 1.0})
    store.record_experiment(run, pd.DataFrame(rows))


def seed_market(store: Store, *, locked: bool = False, missing_target: bool = False) -> None:
    dates = ["20260804", *[f"202608{day:02d}" for day in range(5, 15)]]
    store.upsert("trade_cal", pd.DataFrame([{"exchange": "SSE", "cal_date": d, "is_open": 1} for d in dates]), keys=("exchange", "cal_date"))
    bars = []
    for i, date in enumerate(dates[1:], 1):
        if missing_target and date == "20260807":
            continue
        open_price = 10.0 if i == 1 else 10.0 + i
        bars.append({
            "ts_code": "000001.SZ", "trade_date": date, "open": open_price,
            "high": 10.0 if locked and i == 1 else 11.0,
            "low": 10.0 if locked and i == 1 else 9.0,
            "close": 10.0 if locked and i == 1 else (12.0 if i == 1 else open_price),
        })
    store.upsert("daily", pd.DataFrame(bars), keys=("ts_code", "trade_date"))
    limit = 10.0 if locked else 11.0
    store.upsert("daily_limit", pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260805", "up_limit": limit, "down_limit": 9.0}]), keys=("ts_code", "trade_date"))

def test_calculates_new_horizons_and_sessions(tmp_path):
    with Store(tmp_path / "returns.duckdb") as store:
        seed_run(store)
        seed_market(store)
        result = calculate_experiment_returns(store)
        rows = store.experiment_returns(run_id="run-1", group_name="rule")
        by_horizon = {row["horizon"]: row for row in rows}
        assert set(by_horizon) == set(HORIZONS)
        assert by_horizon["t1_close"]["gross_return"] == pytest.approx(12.0 / 10.0 - 1)
        assert by_horizon["t2_open"]["gross_return"] == pytest.approx(12.0 / 10.0 - 1)
        assert by_horizon["t10_open"]["sell_session"] == "open"
        assert result.rows_written == 40


def test_missing_future_locked_and_target_statuses_are_not_zero(tmp_path):
    with Store(tmp_path / "returns-status.duckdb") as store:
        seed_run(store)
        seed_market(store, locked=True, missing_target=True)
        calculate_experiment_returns(store)
        rows = store.experiment_returns(run_id="run-1", group_name="rule")
        assert {row["status"] for row in rows} == {"entry_unavailable"}
        assert all(row["gross_return"] is None for row in rows)
        # 涨停封板就是买不到:成交价必须留空,否则"买到没买到"从库里分不出来。
        assert all(row["entry_price"] is None for row in rows)


def test_portfolio_return_is_unavailable_when_any_slot_is_missing(tmp_path):
    with Store(tmp_path / "returns-partial.duckdb") as store:
        seed_run(store)
        store.con.execute(
            "INSERT INTO experiment_decisions (run_id, group_name, ts_code, name, industry, rank, rule_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["run-1", "rule", "000002.SZ", "缺失", "测试", 2, 0.5],
        )
        seed_market(store)
        store.upsert(
            "daily",
            pd.DataFrame([{
                "ts_code": "000002.SZ", "trade_date": "20260805", "open": 10.0,
                "high": 11.0, "low": 9.0, "close": 10.0,
            }]),
            keys=("ts_code", "trade_date"),
        )
        store.upsert(
            "daily_limit",
            pd.DataFrame([{"ts_code": "000002.SZ", "trade_date": "20260805", "up_limit": 11.0, "down_limit": 9.0}]),
            keys=("ts_code", "trade_date"),
        )
        calculate_experiment_returns(store)
        summary = returns_summary(store, run_id="run-1")
        full_horizon = summary["groups"]["rule"]["t1_close"]
        assert full_horizon["portfolio_gross_return"] == pytest.approx(0.1)
        horizon = summary["groups"]["rule"]["t2_open"]
        assert horizon["portfolio_gross_return"] is None
        assert horizon["planned_count"] == 2
        assert horizon["measurable_count"] == 1
        assert horizon["coverage"] == pytest.approx(0.5)
        assert horizon["status_distribution"] == {"filled": 1, "target_bar_missing": 1}

def test_rerun_is_idempotent_and_summary_excludes_unavailable(tmp_path):
    with Store(tmp_path / "returns-idempotent.duckdb") as store:
        seed_run(store)
        seed_market(store)
        first = calculate_experiment_returns(store)
        second = calculate_experiment_returns(store)
        assert first.rows_written == second.rows_written == 40
        assert store.con.execute("SELECT COUNT(*) FROM experiment_returns").fetchone()[0] == 40
        summary = returns_summary(store, run_id="run-1")
        assert summary["groups"]["rule"]["t1_close"]["measurable_count"] == 1
        assert summary["groups"]["rule"]["t1_close"]["average"] == pytest.approx(0.2)


# ------------------------------------------------------- 可见日期闸门(前视偏差)


def test_entry_date_inside_hidden_window_is_pending_not_unavailable(tmp_path):
    """买入日落在隐藏窗口内:整批挂起,不写 0 收益也不算 unavailable。"""
    with Store(tmp_path / "returns-hidden-entry.duckdb") as store:
        seed_run(store)
        seed_market(store)
        result = calculate_experiment_returns(store, visible_max="20260804")
        rows = store.experiment_returns(run_id="run-1", group_name="rule")
        assert {row["status"] for row in rows} == {"future_not_visible"}
        assert all(row["gross_return"] is None for row in rows)
        assert all(row["entry_price"] is None for row in rows)
        assert all(row["sell_price"] is None for row in rows)
        # reason 不能留 None,页面要显示"为什么还没算"
        assert all(row["reason"] == "future_not_visible" for row in rows)
        assert result.pending == 40
        assert result.unavailable == 0
        assert result.filled == 0


def test_hidden_sell_dates_become_visible_after_widening_the_window(tmp_path):
    """隐藏窗口是"等它变可见",不是永久拒绝:放宽上限后同一批次能算出来。"""
    with Store(tmp_path / "returns-hidden-sell.duckdb") as store:
        seed_run(store)
        seed_market(store)
        gated = calculate_experiment_returns(store, visible_max="20260808")
        rows = {row["horizon"]: row for row in store.experiment_returns(run_id="run-1", group_name="rule")}
        # t1(20260805)~t4(20260808) 可见,t5(20260809)~t10(20260814) 被隐藏
        assert rows["t1_close"]["status"] == "filled"
        assert rows["t1_close"]["gross_return"] == pytest.approx(0.2)
        assert rows["t4_open"]["gross_return"] == pytest.approx(0.4)
        for horizon in ("t5_open", "t6_open", "t7_open", "t8_open", "t9_open", "t10_open"):
            assert rows[horizon]["status"] == "future_not_visible"
            assert rows[horizon]["gross_return"] is None
            assert rows[horizon]["sell_price"] is None
            # 买入日可见,所以入场价照常落库
            assert rows[horizon]["entry_price"] == pytest.approx(10.0)
        assert gated.pending == 4 * 6
        assert gated.filled == 4 * 4

        widened = calculate_experiment_returns(store, visible_max="20260814")
        after = {row["horizon"]: row for row in store.experiment_returns(run_id="run-1", group_name="rule")}
        assert widened.filled == 40
        assert widened.pending == 0
        assert after["t10_open"]["status"] == "filled"
        assert after["t10_open"]["gross_return"] == pytest.approx(20.0 / 10.0 - 1)
        # 幂等:放宽后是覆盖同一批行,不是追加
        assert store.con.execute("SELECT COUNT(*) FROM experiment_returns").fetchone()[0] == 40


def test_hidden_sell_date_bar_is_never_read(tmp_path):
    """给隐藏日期灌一根离谱 K 线:结果里既不该出现它的价格,也不该退化成缺数据。"""
    with Store(tmp_path / "returns-hidden-not-read.duckdb") as store:
        seed_run(store)
        seed_market(store)
        store.upsert(
            "daily",
            pd.DataFrame([{
                "ts_code": "000001.SZ", "trade_date": "20260807", "open": 999.0,
                "high": 1000.0, "low": 998.0, "close": 999.0,
            }]),
            keys=("ts_code", "trade_date"),
        )
        calculate_experiment_returns(store, visible_max="20260806")
        rows = store.experiment_returns(run_id="run-1")
        by_horizon = {row["horizon"]: row for row in rows if row["group_name"] == "rule"}
        hidden = by_horizon["t3_open"]
        assert hidden["sell_date"] == "20260807"
        assert hidden["status"] == "future_not_visible"
        assert hidden["sell_price"] is None
        assert hidden["gross_return"] is None
        # 那根离谱 K 线的价格没有出现在任何一行里 → 隐藏日期的行情确实没被读
        assert all(row["sell_price"] != 999.0 for row in rows)
        assert all(row["gross_return"] is None or row["gross_return"] < 1.0 for row in rows)
        # 隐藏日期也没有被当成"缺数据"
        assert "target_bar_missing" not in {row["status"] for row in rows}
 
def test_missing_required_limit_table_fails_explicitly_without_partial_returns(tmp_path):
    with Store(tmp_path / "returns-missing-table.duckdb") as store:
        seed_run(store)
        seed_market(store)
        store.con.execute("DROP TABLE daily_limit")

        with pytest.raises(RuntimeError, match="daily_limit"):
            calculate_experiment_returns(store, run_id="run-1")

        assert store.con.execute("SELECT COUNT(*) FROM experiment_returns").fetchone()[0] == 0


def test_future_entry_is_pending_and_never_written_as_zero(tmp_path):
    with Store(tmp_path / "returns-future-entry.duckdb") as store:
        seed_run(store)
        # The calendar knows the future entry session, but local daily data ends on as_of.
        store.upsert(
            "trade_cal",
            pd.DataFrame([
                {"exchange": "SSE", "cal_date": "20260804", "is_open": 1},
                {"exchange": "SSE", "cal_date": "20260805", "is_open": 1},
            ]),
            keys=("exchange", "cal_date"),
        )
        store.upsert(
            "daily",
            pd.DataFrame([{
                "ts_code": "000001.SZ", "trade_date": "20260804", "open": 10.0,
                "high": 10.0, "low": 10.0, "close": 10.0,
            }]),
            keys=("ts_code", "trade_date"),
        )
        result = calculate_experiment_returns(store, run_id="run-1")
        rows = store.experiment_returns(run_id="run-1")
        assert result.pending == 40
        assert {row["status"] for row in rows} == {"future_not_reached"}
        assert all(row["gross_return"] is None for row in rows)

def test_missing_trade_calendar_fails_without_partial_returns(tmp_path):
    """缺交易日历必须显式失败,且不能先写入部分 horizon。"""
    with Store(tmp_path / "returns-missing-calendar.duckdb") as store:
        seed_run(store)
        seed_market(store)
        store.con.execute("DROP TABLE trade_cal")

        with pytest.raises(RuntimeError, match="trade_cal"):
            calculate_experiment_returns(store, run_id="run-1")

        assert store.con.execute("SELECT COUNT(*) FROM experiment_returns").fetchone()[0] == 0


def test_missing_entry_limit_is_pending_without_zero_return(tmp_path):
    """买入日缺涨跌停价只能待处理,绝不能伪装成 0 收益。"""
    with Store(tmp_path / "returns-missing-entry-limit.duckdb") as store:
        seed_run(store)
        seed_market(store)
        store.con.execute("DELETE FROM daily_limit")

        result = calculate_experiment_returns(store, run_id="run-1")
        rows = store.experiment_returns(run_id="run-1", group_name="rule")
        assert result.pending == 4 * len(HORIZONS)
        assert result.unavailable == 0
        assert {row["status"] for row in rows} == {"pending_entry"}
        assert {row["reason"] for row in rows} == {"limit_price_missing"}
        assert all(row["entry_price"] is None for row in rows)
        assert all(row["gross_return"] is None for row in rows)
