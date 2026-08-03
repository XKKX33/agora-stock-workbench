"""训练编排:数据集 → 净化滚动切分 → 拟合 → 样本外评估 → 落盘产物。

全流程只有一个目的:回答"这套因子在历史上对 T+N 收益有没有区分度"。
不是为了预测涨跌幅,也不是为了替代 score.py 的规则打分。

评估口径为什么必须是样本外
--------------------------
同一批数据上训练再评估,IC 可以轻松做到 0.3 以上,毫无意义。
本模块所有对外指标都来自 walk-forward 的**测试窗口**,训练窗口的指标
只作为过拟合诊断(train_ic 远高于 test_ic 就是警报),不进产物门槛。

净化(purge)在哪一步
--------------------
splits.purged_walk_forward 已经把训练集末尾 horizon 天挖掉:
t 时刻的样本,标签跨 t..t+N,若 t+N 落进测试窗口,训练集就偷看了答案。
这一步不是可选优化,是正确性前提。

聚合方式
--------
各折的测试集预测**拼起来再统一算指标**,而不是把各折 IC 求平均。
按日算 IC 再跨日取均值,本身已是逐日口径;把折内均值再平均会给
样本少的折过高权重。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..db import Store
from . import registry
from .dataset import DatasetReport, META_COLUMNS, build_dataset, feature_columns
from .metrics import EvalResult, evaluate_predictions
from .model import make_model
from .splits import Fold, assert_no_leakage, purged_walk_forward, split_frame
from .labels import HORIZONS

# 一折训练集至少要有的样本数。低于此值拟合出来的权重纯属噪声。
MIN_FOLD_SAMPLES = 120


@dataclass
class TrainReport:
    """训练全过程的可解释记录。"""

    horizon: str
    backend: str
    features: List[str] = field(default_factory=list)
    n_folds: int = 0
    skipped_folds: Dict[str, int] = field(default_factory=dict)
    fold_metrics: List[dict] = field(default_factory=list)
    oos: Optional[EvalResult] = None
    train_ic: Optional[float] = None
    dataset: Optional[DatasetReport] = None
    artifact_path: Optional[str] = None

    def note_skip(self, reason: str) -> None:
        self.skipped_folds[reason] = self.skipped_folds.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "backend": self.backend,
            "features": list(self.features),
            "n_folds": self.n_folds,
            "skipped_folds": dict(self.skipped_folds),
            "fold_metrics": list(self.fold_metrics),
            "oos": self.oos.as_dict() if self.oos else None,
            "train_ic": self.train_ic,
            "overfit_gap": self._overfit_gap(),
            "dataset": self.dataset.as_dict() if self.dataset else None,
            "artifact_path": self.artifact_path,
        }

    def _overfit_gap(self) -> Optional[float]:
        """样本内 IC 减样本外 IC。缺一边就是 None,不猜。"""
        if self.oos is None or self.oos.ic_mean is None or self.train_ic is None:
            return None
        return float(self.train_ic - self.oos.ic_mean)


def _matrix(frame: pd.DataFrame, features: List[str]) -> np.ndarray:
    return frame.reindex(columns=features).to_numpy(dtype="float64")


def train_on_frame(
    samples: pd.DataFrame,
    *,
    horizon: str = "ret5",
    backend: str = "auto",
    n_splits: int = 3,
    embargo_days: int = 0,
    min_train_days: int = 20,
    top_k: int = 5,
    model_params: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[Any], TrainReport]:
    """在已构建好的样本表上做净化滚动训练。

    samples 需含 META_COLUMNS(至少 ts_code / as_of / label)与因子列。
    返回 (在全量数据上重训的最终模型, 训练报告)。
    样本不足时返回 (None, 报告) —— 不返回一个训不出来的空模型。
    """
    if horizon not in HORIZONS:
        raise ValueError(f"未知期限: {horizon},可选 {sorted(HORIZONS)}")
    horizon_days = HORIZONS[horizon]
    features = feature_columns(samples) if not samples.empty else []
    probe = make_model(backend, **(model_params or {}))
    report = TrainReport(
        horizon=horizon,
        backend=getattr(probe, "backend", "unknown"),
        features=features,
    )
    if samples is None or samples.empty:
        report.note_skip("empty_dataset")
        return None, report

    # 只有标签算得出来的行能参与训练。丢弃行数由 dataset 报告解释,
    # 这里不再重复统计原因,避免两处口径打架。
    labeled = samples.dropna(subset=["label"]).copy()
    if labeled.empty:
        report.note_skip("no_labeled_samples")
        return None, report

    days = sorted({str(d) for d in labeled["as_of"]})
    folds = purged_walk_forward(
        days,
        horizon_days=horizon_days,
        n_splits=n_splits,
        min_train_days=min_train_days,
        embargo_days=embargo_days,
    )
    if not folds:
        report.note_skip("insufficient_days")
        return None, report

    oos_parts: List[pd.DataFrame] = []
    train_ics: List[float] = []
    for fold in folds:
        assert_no_leakage(fold, horizon_days, days)
        train_df, test_df = split_frame(labeled, fold, day_col="as_of")
        if len(train_df) < MIN_FOLD_SAMPLES:
            report.note_skip("train_too_small")
            continue
        if test_df.empty:
            report.note_skip("empty_test")
            continue

        model = make_model(backend, **(model_params or {}))
        model.fit(_matrix(train_df, features), train_df["label"].to_numpy(dtype="float64"))

        test_pred = test_df[["ts_code", "as_of", "label"]].copy()
        test_pred["pred"] = model.predict(_matrix(test_df, features))
        oos_parts.append(test_pred)

        fold_eval = evaluate_predictions(test_pred, top_k=top_k)
        entry = fold.as_dict()
        entry["metrics"] = fold_eval.as_dict()
        report.fold_metrics.append(entry)
        report.n_folds += 1

        # 样本内 IC 只用于过拟合诊断,不参与达标判定
        train_pred = train_df[["ts_code", "as_of", "label"]].copy()
        train_pred["pred"] = model.predict(_matrix(train_df, features))
        train_eval = evaluate_predictions(train_pred, top_k=top_k)
        if train_eval.ic_mean is not None:
            train_ics.append(float(train_eval.ic_mean))

    if not oos_parts:
        return None, report

    oos = pd.concat(oos_parts, ignore_index=True)
    report.oos = evaluate_predictions(oos, top_k=top_k)
    report.train_ic = float(np.mean(train_ics)) if train_ics else None

    # 最终产物在全量已标注数据上重训:评估用样本外,上线用全部信息。
    # 这是标准做法,但必须记住——产物的可信度来自上面那份样本外指标,
    # 而不是这次重训本身。
    final = make_model(backend, **(model_params or {}))
    final.fit(_matrix(labeled, features), labeled["label"].to_numpy(dtype="float64"))
    return final, report


def train_from_store(
    store: Store,
    *,
    universe_cfg: Dict[str, Any],
    name: str = "factor_ml",
    horizon: str = "ret5",
    backend: str = "auto",
    max_days: int = 60,
    stride: int = 1,
    candidate_limit: int = 260,
    history_bars: int = 150,
    n_splits: int = 3,
    embargo_days: int = 0,
    top_k: int = 5,
    artifact_base: Optional[str] = None,
    save: bool = True,
    model_params: Optional[Dict[str, Any]] = None,
) -> TrainReport:
    """端到端:重放历史 → 训练 → 评估 → (可选)落盘。

    store 必须以只读方式打开(ensure_schema=False),本函数不写库。
    产物落到 data/models/<name>.json,与数据库解耦。
    """
    samples, dataset_report = build_dataset(
        store,
        universe_cfg=universe_cfg,
        candidate_limit=candidate_limit,
        history_bars=history_bars,
        horizon=horizon,
        max_days=max_days,
        stride=stride,
    )
    model, report = train_on_frame(
        samples,
        horizon=horizon,
        backend=backend,
        n_splits=n_splits,
        embargo_days=embargo_days,
        top_k=top_k,
        model_params=model_params,
    )
    report.dataset = dataset_report

    if model is None or not save:
        return report

    # 样本外指标之外,把训练侧 IC 与过拟合差值也写进产物:
    # 光看样本外 IC 低,分不清"因子本身没用"还是"模型把训练集背下来了",
    # 而这两种情况下一步该做的事完全不同。
    metrics = report.oos.as_dict() if report.oos else {}
    metrics["train_ic"] = report.train_ic
    metrics["overfit_gap"] = report.as_dict()["overfit_gap"]
    metrics["skipped_folds"] = dict(report.skipped_folds)
    path = registry.save_artifact(
        model,
        name=name,
        horizon=horizon,
        features=report.features,
        metrics=metrics,
        dataset=dataset_report.as_dict(),
        folds=report.fold_metrics,
        params={
            "backend": report.backend,
            "n_splits": n_splits,
            "embargo_days": embargo_days,
            "stride": stride,
            "max_days": max_days,
            "candidate_limit": candidate_limit,
            "top_k": top_k,
            **(model_params or {}),
        },
        base=artifact_base,
    )
    report.artifact_path = str(path)
    return report


def predict_frame(
    artifact: registry.Artifact, frame: pd.DataFrame
) -> tuple[pd.Series, List[str]]:
    """用产物给一份截面打分。返回 (分数, 缺失特征列表)。

    缺失的特征列不静默补 0:补 0 在归一化口径下等于"该因子最差",
    会系统性压低那些票的分数。这里补中性值 NaN,交给模型的
    训练集均值填充逻辑处理,并把缺失列名如实返回给调用方展示。
    """
    model = artifact.load()
    features = artifact.features
    if not features:
        raise ValueError("产物没有记录特征列,无法安全预测")
    missing = [f for f in features if f not in frame.columns]
    aligned = frame.reindex(columns=features)
    scores = model.predict(aligned.to_numpy(dtype="float64"))
    return pd.Series(scores, index=frame.index, dtype="float64"), missing
