"""训练机器学习复核模型:重放历史截面 → 净化滚动评估 → 落盘产物。

只读 DuckDB,不联网、不写库。产物落到 data/models/<name>.json。

用法:
    python tools/train_ml.py                      # 默认 ret5,近 60 个截面
    python tools/train_ml.py --horizon ret10 --max-days 120
    python tools/train_ml.py --stride 2 --backend ridge
    python tools/train_ml.py --dry-run            # 只看指标,不落盘
    python tools/train_ml.py --end 20260706       # 指定采样截止日(必须 <= 可见日)

采样截止日默认取防前视可见日(基准日往前退 N 个开市日),不是库里最新交易日:
训练用了隐藏窗口的行情,等于让模型先看当时还没落地的数据,再拿它给可见日
打分,泄漏发生在训练里、事后从指标上看不出来。

关于耗时:重放是 O(截面数 × 候选数) 次日线查询。60 个截面约 40~60 秒,
120 个截面翻倍。--stride 2 可在覆盖同样时间跨度下把成本减半,
代价是截面数减半(相邻交易日的因子值高度重叠,损失其实有限)。

关于结果为负
------------
样本外 IC 为负是**有效结论**,不是失败:它说明这套因子在该期限上
没有正向区分度,产物会被登记为 pending 而不会上线。把它当成"没训成"
去反复调参直到 IC 转正,就是在用测试集过拟合。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import load_settings, load_strategy, resolve_path  # noqa: E402
from engine.db import Store  # noqa: E402
from engine.ml import registry  # noqa: E402
from engine.ml.labels import HORIZONS  # noqa: E402
from engine.ml.train import train_from_store  # noqa: E402
from engine.schedule import load_schedule_config, normalize_trade_date  # noqa: E402
from engine.visibility import (  # noqa: E402
    LookaheadBlocked,
    ensure_visible,
    require_visible_as_of,
    resolve_window,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="训练因子机器学习复核模型")
    p.add_argument("--name", default="factor_ml", help="产物名(与页面读取的名字一致)")
    p.add_argument(
        "--strategy",
        default=None,
        help="策略配置名(取 universe 口径),默认取 settings.engine.default_strategy",
    )
    p.add_argument("--horizon", default="ret5", choices=sorted(HORIZONS), help="标签期限")
    p.add_argument("--backend", default="auto", choices=["auto", "ridge", "lightgbm"])
    p.add_argument("--max-days", type=int, default=60, help="最多重放多少个交易截面")
    p.add_argument("--stride", type=int, default=1, help="隔几个交易日取一个截面")
    p.add_argument("--n-splits", type=int, default=3, help="滚动折数")
    p.add_argument("--embargo-days", type=int, default=0, help="额外隔离天数(滚动特征用)")
    p.add_argument("--candidate-limit", type=int, default=260, help="每个截面的候选池上限")
    p.add_argument("--top-k", type=int, default=5, help="top 桶大小(贴近实盘只买前几名)")
    p.add_argument(
        "--db",
        default=None,
        help="改用指定的 DuckDB 文件(默认取 settings 里的 data.db_path)。"
             "Store 是文件级读写连接,只是不执行 DDL;要保证线上库字节不变,"
             "就先把库复制一份再用本参数指向副本。产物落点不受影响。",
    )
    p.add_argument(
        "--end",
        default=None,
        help="采样截止日 YYYYMMDD(必须 <= 可见日),默认取可见日",
    )
    p.add_argument("--dry-run", action="store_true", help="只评估不落盘")
    return p.parse_args()


def resolve_end(
    store: Store, settings: dict, *, exchange: str, requested: str | None
) -> str:
    """算出本次训练的采样截止日:默认可见日,显式日期必须 <= 可见日。

    与 run_scan / review / postmortem 的命令行同一条纪律:engine 层不做
    可见性判断,由入口先算出可见日再传下去。
    """
    window = resolve_window(store, settings, exchange=exchange)
    if requested is None:
        return require_visible_as_of(window)
    return ensure_visible(normalize_trade_date(requested), window)


def main() -> int:
    args = _parse_args()
    settings = load_settings()
    # 策略名与 run_scan 同一口径:config/strategies/ 下并没有 default.yaml,
    # 之前硬写 "default" 让不带 --strategy 的默认跑法直接 FileNotFoundError。
    strategy_name = args.strategy or settings["engine"]["default_strategy"]
    strategy = load_strategy(strategy_name)
    # settings.yaml 里的键是 data.db_path(engine/run_scan、review、postmortem 都读这个)。
    # 此处原先写成 settings["storage"],不传 --db 就 KeyError——上次训练一直显式传了
    # 副本路径,所以这条默认分支从未被走到,缺陷也就没暴露。
    db_path = Path(args.db) if args.db else resolve_path(settings["data"]["db_path"])
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2

    # ensure_schema=False:读路径绝不建表、绝不写库
    with Store(db_path, ensure_schema=False) as store:
        config = load_schedule_config(settings)
        try:
            end_day = resolve_end(
                store, settings, exchange=config.exchange, requested=args.end
            )
        except LookaheadBlocked as exc:
            print(f"拒绝训练: {exc}", file=sys.stderr)
            return 2
        report = train_from_store(
            store,
            universe_cfg=strategy.get("universe", {}),
            name=args.name,
            horizon=args.horizon,
            backend=args.backend,
            max_days=args.max_days,
            stride=args.stride,
            candidate_limit=args.candidate_limit,
            n_splits=args.n_splits,
            embargo_days=args.embargo_days,
            top_k=args.top_k,
            save=not args.dry_run,
            end=end_day,
        )

    payload = report.as_dict()
    # daily_ic 逐日明细太长,命令行只打摘要;完整内容已在产物 JSON 里
    oos = dict(payload.get("oos") or {})
    oos.pop("daily_ic", None)
    print(json.dumps({
        "backend": payload["backend"],
        "horizon": payload["horizon"],
        "n_folds": payload["n_folds"],
        "skipped_folds": payload["skipped_folds"],
        "features": payload["features"],
        "dataset": payload["dataset"],
        "oos": oos,
        "train_ic": payload["train_ic"],
        "overfit_gap": payload["overfit_gap"],
        "artifact_path": payload["artifact_path"],
    }, ensure_ascii=False, indent=2))

    if report.oos is None:
        print("\n未产出样本外指标:样本或截面不足,产物未启用。", file=sys.stderr)
        return 1

    # 如实告知门槛判定结果,别让人以为"训完了就能用"
    artifact = registry.load_artifact(args.name) if not args.dry_run else None
    if artifact is not None:
        state = registry.evaluate_availability(artifact)
        print(f"\n可用性: {state['availability']}")
        if state["availability"] != "available":
            print(f"原因: {state['reason']}")
    gap = payload.get("overfit_gap")
    if gap is not None and gap > 0.1:
        print(f"提示: 样本内外 IC 差 {gap:.3f},存在明显过拟合。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
