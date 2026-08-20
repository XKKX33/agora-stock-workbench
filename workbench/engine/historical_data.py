"""历史信号日数据准备入口。

本模块只负责把指定交易日的扫描输入准备完整，不评分、不写选股结果、不调用 Agent。
历史日期的可见性校验由命令行入口执行；业务函数可被一键流程或测试复用。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_settings, resolve_path
from .db import Store
from .run_scan import prepare_scan_data, validate_scan_integrity
from .schedule import normalize_trade_date
from .visibility import LookaheadBlocked, ensure_visible, resolve_window


def prepare_historical_data(
    *,
    db_path: str | Path,
    trade_date: str,
    strategy_name: str | None = None,
    online: bool = True,
) -> dict[str, Any]:
    """准备一个历史信号日的完整扫描输入，不执行评分和落盘选股结果。"""
    if not isinstance(trade_date, str) or not trade_date.strip():
        raise ValueError("trade_date 必须是非空交易日字符串")
    normalized = normalize_trade_date(trade_date)
    prepared = prepare_scan_data(
        strategy_name=strategy_name,
        online=online,
        db_path=str(db_path),
        as_of=normalized,
    )
    integrity = validate_scan_integrity(prepared, require_complete_sources=online)
    return {
        "as_of": prepared.as_of,
        "strategy": prepared.strategy_name,
        "snapshot_count": prepared.snapshot_count,
        "candidate_count": len(prepared.candidates),
        "context_count": len(prepared.contexts),
        "data_cutoff_at": prepared.data_cutoff_at,
        "data_quality": prepared.data_quality,
        "integrity": integrity,
    }


def _visible_trade_date(settings: dict[str, Any], db_path: Path, requested: str) -> str:
    with Store(db_path, ensure_schema=False) as store:
        window = resolve_window(store, settings, exchange="SSE")
    try:
        return ensure_visible(normalize_trade_date(requested), window)
    except LookaheadBlocked as exc:
        raise SystemExit(f"拒绝准备历史数据: {exc}") from exc


def _cli() -> None:
    parser = argparse.ArgumentParser(description="准备历史信号日扫描数据")
    parser.add_argument("--trade-date", required=True, help="信号日 YYYYMMDD")
    parser.add_argument("--strategy", default=None, help="策略名称")
    parser.add_argument("--offline", action="store_true", help="只使用本地数据")
    parser.add_argument("--output", default=None, help="额外导出 JSON 文件")
    args = parser.parse_args()

    settings = load_settings()
    db_path = Path(resolve_path(settings["data"]["db_path"]))
    trade_date = _visible_trade_date(settings, db_path, args.trade_date)
    result = prepare_historical_data(
        db_path=db_path,
        trade_date=trade_date,
        strategy_name=args.strategy,
        online=not args.offline,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    _cli()
