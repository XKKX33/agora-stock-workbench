"""收盘后任务链的日期口径回归。

锁一件事:链条的扫描截面必须等于传入的目标交易日。这个日期由调用方按防前视
口径算出(命令行走 require_visible_as_of),run_close_pipeline 不能自己去取
"最新交易日"——那是隐藏窗口里的日期,一旦回退,整条防前视闸门就白设了。

运行:
    python -m pytest tests/test_close_pipeline.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import engine.close_pipeline as close_pipeline
from engine.close_pipeline import (
    STATUS_OK,
    STEP_BACKFILL,
    STEP_SCAN,
    PipelineResult,
    StepResult,
    run_close_pipeline,
)
from engine.db import Store
from engine.visibility import DEFAULT_DELAY_SESSIONS
from tests.test_run_scan_offline import _TRADE_DATES, _seed_db

# 可见日 = 基准日往前退 DEFAULT_DELAY_SESSIONS 个开市日。这里绑代码默认值而不是
# settings.yaml:隐藏窗口是运营可调参数(舆情源只能采实时数据,生产上可能调成 0
# 让选股截面与舆情对齐),而这条用例锁的是"截面等于传入日期"这个代码契约。
VISIBLE_AS_OF = _TRADE_DATES[-(DEFAULT_DELAY_SESSIONS + 1)]

pytestmark = pytest.mark.integration


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "market.duckdb")
    with Store(path) as store:
        _seed_db(store)
    return path


@pytest.fixture(autouse=True)
def _stub_news(monkeypatch: pytest.MonkeyPatch) -> None:
    """舆情采集要联网。日期口径与它无关,换成固定替身。"""
    monkeypatch.setattr(
        close_pipeline,
        "_collect_news_step",
        lambda **_kwargs: StepResult(
            name=close_pipeline.STEP_NEWS,
            status=STATUS_OK,
            detail="测试替身:不采集",
            data={"fetched": 0, "stored": 0, "duplicates": 0},
        ),
    )


def _run(db_path: str) -> PipelineResult:
    return run_close_pipeline(
        db_path=db_path,
        strategy="strong_mainup",
        trade_date=VISIBLE_AS_OF,
        online=False,
    )


def _step(result: PipelineResult, name: str) -> StepResult:
    for step in result.steps:
        if step.name == name:
            return step
    raise AssertionError(f"链条缺少步骤 {name}")


def test_scan_uses_the_given_trade_date(db_path: str) -> None:
    """截面就是传入的可见日,不能回退成库里最新交易日。"""
    # 前提:可见日与最新交易日不同,否则这条测试抓不到回退
    assert VISIBLE_AS_OF != _TRADE_DATES[-1]

    result = _run(db_path)

    assert _step(result, STEP_SCAN).data["as_of"] == VISIBLE_AS_OF
    assert result.trade_date == VISIBLE_AS_OF
    with Store(db_path) as store:
        runs = store.scan_runs()
        assert list(runs["as_of"]) == [VISIBLE_AS_OF]


def test_backfill_step_caps_at_the_visible_date(db_path: str) -> None:
    """回填步必须带可见上限,否则会用隐藏窗口里的收盘价填 retN。"""
    backfill = _step(_run(db_path), STEP_BACKFILL)

    assert backfill.data["visible_max"] == VISIBLE_AS_OF
