"""可见日期闸门的单元测试:窗口计算、配置校验、拒绝语义、补齐目标日期。

window_for / backfill_sessions 一律用真实 Store + 真实 trade_cal 行,
不用假 store 对象——否则测的是 mock 的行为,不是生产查询的行为。
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine.db import Store
from engine.visibility import (
    DEFAULT_DELAY_SESSIONS,
    LookaheadBlocked,
    REASON_CALENDAR_MISSING,
    REASON_INSUFFICIENT,
    REASON_NO_BASE,
    VisibilityConfigError,
    VisibilityWindow,
    backfill_sessions,
    build_window,
    ensure_visible,
    load_delay_sessions,
    require_visible_as_of,
    window_for,
)


def _seed_calendar(store: Store, dates: list[str], *, closed: tuple[str, ...] = ()) -> None:
    """往 trade_cal 塞真实行;closed 里的日期写 is_open=0,用于验证只数开市日。"""
    store.upsert(
        "trade_cal",
        pd.DataFrame(
            [
                {"exchange": "SSE", "cal_date": date, "is_open": 0 if date in closed else 1}
                for date in dates
            ]
        ),
        keys=("exchange", "cal_date"),
    )


# ---------------------------------------------------------------- build_window


def test_build_window_hides_the_latest_sessions():
    sessions = ["20260801", "20260802", "20260803", "20260804", "20260805"]
    window = build_window(sessions, 2)
    assert window.available is True
    assert window.base_session == "20260805"
    assert window.visible_as_of == "20260803"
    assert window.hidden_sessions == ("20260804", "20260805")
    assert window.reason is None
    assert window.as_dict()["hidden_count"] == 2


def test_build_window_is_unavailable_when_history_is_exactly_one_short():
    # 3 个开市日想退 3 个 → 差一个,窗口不可用,不能拿最早那天凑数。
    window = build_window(["20260803", "20260804", "20260805"], 3)
    assert window.available is False
    assert window.visible_as_of is None
    assert window.base_session == "20260805"
    assert window.reason == REASON_INSUFFICIENT
    assert window.hidden_sessions == ("20260803", "20260804", "20260805")


def test_build_window_without_sessions_has_no_base():
    window = build_window([], 20)
    assert window.available is False
    assert window.base_session is None
    assert window.reason == REASON_NO_BASE


def test_build_window_with_single_session_far_short_is_insufficient():
    window = build_window(["20260805"], 20)
    assert window.available is False
    assert window.reason == REASON_INSUFFICIENT


def test_build_window_with_zero_delay_shows_the_base_session():
    window = build_window(["20260804", "20260805"], 0)
    assert window.visible_as_of == "20260805"
    assert window.base_session == "20260805"
    assert window.hidden_sessions == ()
    assert window.reason is None


def test_build_window_rejects_negative_delay():
    with pytest.raises(VisibilityConfigError):
        build_window(["20260805"], -1)


# ---------------------------------------------------------------- load_delay_sessions


def test_load_delay_sessions_defaults_to_twenty():
    assert load_delay_sessions({}) == DEFAULT_DELAY_SESSIONS
    assert load_delay_sessions({"data": {}}) == DEFAULT_DELAY_SESSIONS
    assert load_delay_sessions({"data": {"visibility_delay_sessions": 5}}) == 5
    assert load_delay_sessions({"data": {"visibility_delay_sessions": 0}}) == 0


@pytest.mark.parametrize("raw", ["20", -1, True, 20.0, None])
def test_load_delay_sessions_rejects_illegal_values(raw):
    with pytest.raises(VisibilityConfigError):
        load_delay_sessions({"data": {"visibility_delay_sessions": raw}})


# ---------------------------------------------------------------- window_for


def test_window_for_counts_only_open_sessions(tmp_path):
    with Store(tmp_path / "visibility-window.duckdb") as store:
        # 20260808 记为休市:它不能被算进"往前退 2 个开市日"。
        _seed_calendar(
            store,
            ["20260804", "20260805", "20260806", "20260807", "20260808", "20260810"],
            closed=("20260808",),
        )
        window = window_for(
            store, exchange="SSE", delay_sessions=2, base_session="20260810"
        )
    assert window.base_session == "20260810"
    assert window.visible_as_of == "20260806"
    assert window.hidden_sessions == ("20260807", "20260810")


def test_window_for_reports_calendar_missing_base(tmp_path):
    with Store(tmp_path / "visibility-missing.duckdb") as store:
        _seed_calendar(store, ["20260801", "20260802", "20260803"])
        window = window_for(
            store, exchange="SSE", delay_sessions=1, base_session="20260810"
        )
    assert window.available is False
    assert window.reason == REASON_CALENDAR_MISSING
    assert window.base_session == "20260810"
    assert "trade_cal" in window.unavailable_detail()


def test_window_for_without_base_session_has_no_base(tmp_path):
    with Store(tmp_path / "visibility-nobase.duckdb") as store:
        window = window_for(
            store, exchange="SSE", delay_sessions=20, base_session=None
        )
    assert window.reason == REASON_NO_BASE
    assert window.available is False


# ---------------------------------------------------------------- 拒绝语义


def test_ensure_visible_blocks_dates_inside_the_hidden_window():
    window = build_window(["20260801", "20260802", "20260803"], 2)
    assert ensure_visible("20260801", window) == "20260801"
    with pytest.raises(LookaheadBlocked) as excinfo:
        ensure_visible("20260803", window)
    assert excinfo.value.code == "lookahead_blocked"
    assert "20260801" in str(excinfo.value)


def test_ensure_visible_on_unavailable_window_reports_window_error():
    window = build_window([], 20)
    with pytest.raises(LookaheadBlocked) as excinfo:
        ensure_visible("20260801", window)
    assert excinfo.value.code == "visibility_window_unavailable"
    assert excinfo.value.reason == REASON_NO_BASE


def test_require_visible_as_of_never_falls_back_to_base_session():
    window = VisibilityWindow("20260805", None, 20, ("20260805",), REASON_INSUFFICIENT)
    with pytest.raises(LookaheadBlocked) as excinfo:
        require_visible_as_of(window)
    assert excinfo.value.code == "visibility_window_unavailable"
    assert excinfo.value.reason == REASON_INSUFFICIENT
    assert "20260805" not in str(excinfo.value)


# ---------------------------------------------------------------- backfill_sessions


def test_backfill_sessions_ends_at_visible_as_of_and_is_ascending(tmp_path):
    dates = [f"202608{day:02d}" for day in range(1, 11)]
    with Store(tmp_path / "visibility-backfill.duckdb") as store:
        _seed_calendar(store, dates)
        window = window_for(
            store, exchange="SSE", delay_sessions=2, base_session="20260810"
        )
        sessions = backfill_sessions(store, exchange="SSE", window=window, count=4)
    assert window.visible_as_of == "20260808"
    assert sessions == ["20260805", "20260806", "20260807", "20260808"]
    assert sessions == sorted(sessions)
    assert sessions[-1] == window.visible_as_of


def test_backfill_sessions_refuses_unavailable_window_and_nonpositive_count(tmp_path):
    with Store(tmp_path / "visibility-backfill-bad.duckdb") as store:
        _seed_calendar(store, ["20260801", "20260802"])
        good = window_for(store, exchange="SSE", delay_sessions=1, base_session="20260802")
        with pytest.raises(ValueError):
            backfill_sessions(store, exchange="SSE", window=good, count=0)
        bad = window_for(store, exchange="SSE", delay_sessions=5, base_session="20260802")
        with pytest.raises(LookaheadBlocked) as excinfo:
            backfill_sessions(store, exchange="SSE", window=bad, count=3)
    assert excinfo.value.code == "visibility_window_unavailable"



# ------------------------------------------------- 因子训练采样截止日(tools/train_ml)


def _seed_daily(store: Store, dates: list[str]) -> None:
    """往 daily 塞最小可用行:resolve_window 的基准日是从这张表推出来的。"""
    store.upsert(
        "daily",
        pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "trade_date": date,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "pre_close": 10.0,
                    "pct_chg": 2.0,
                    "vol": 10_000.0,
                    "amount": 100_000.0,
                }
                for date in dates
            ]
        ),
        keys=("ts_code", "trade_date"),
    )


def test_train_end_defaults_to_visible_session_and_blocks_hidden_dates(tmp_path):
    """训练截止日的三个分支:默认取可见日、显式合法照用、显式越界拒绝。

    训练看到隐藏窗口的行情,等于用当时还没落地的数据拟合,再拿这个模型给
    可见日打分——泄漏发生在训练里,事后从指标上看不出来。
    """
    from tools.train_ml import resolve_end

    settings = {"data": {"visibility_delay_sessions": 2, "min_daily_rows": 0}}
    sessions = ["20260801", "20260802", "20260803", "20260804", "20260805"]
    with Store(tmp_path / "ml-train-gate.duckdb") as store:
        _seed_calendar(store, sessions)
        _seed_daily(store, sessions)
        assert resolve_end(store, settings, exchange="SSE", requested=None) == "20260803"
        assert (
            resolve_end(store, settings, exchange="SSE", requested="20260802")
            == "20260802"
        )
        with pytest.raises(LookaheadBlocked) as excinfo:
            resolve_end(store, settings, exchange="SSE", requested="20260805")
    assert excinfo.value.code == "lookahead_blocked"
    assert "20260803" in str(excinfo.value)


def test_build_dataset_caps_sampling_at_the_given_end(tmp_path):
    from engine.ml.dataset import build_dataset

    sessions = ["20260801", "20260802", "20260803", "20260804", "20260805"]
    with Store(tmp_path / "ml-dataset-end.duckdb") as store:
        _seed_calendar(store, sessions)
        _seed_daily(store, sessions)
        _, report = build_dataset(
            store, universe_cfg={}, horizon="ret1", max_days=3, end="20260803"
        )
    assert report.end_day == "20260803"
    # ret1 的标签要等 T+1,所以最后一个可采样截面是 end 往前退 1 个开市日。
    assert report.label_cutoff == "20260802"


def test_build_dataset_without_end_stays_on_the_pure_data_max(tmp_path):
    """engine 层不做可见性判断:不传 end 就是库里最新交易日。

    闸门只放在入口(tools/train_ml),两处都判会出现两套口径。
    """
    from engine.ml.dataset import build_dataset

    sessions = ["20260801", "20260802", "20260803", "20260804", "20260805"]
    with Store(tmp_path / "ml-dataset-nomax.duckdb") as store:
        _seed_calendar(store, sessions)
        _seed_daily(store, sessions)
        _, report = build_dataset(
            store, universe_cfg={}, horizon="ret1", max_days=3
        )
    assert report.end_day == "20260805"
    assert report.label_cutoff == "20260804"