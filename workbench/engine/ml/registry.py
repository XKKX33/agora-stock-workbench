"""模型产物登记:落盘、读取、可用性判定。

为什么产物必须自带"体检报告"
----------------------------
一个模型文件本身不说明它能不能用。同一份权重,在 8 个截面上训出来的
和在 200 个截面上训出来的,可信度差一个量级。所以产物里除了权重,
必须钉住四类元数据:

  1. backend    —— 实际用的后端(ridge_numpy / lightgbm),不许美化
  2. dataset    —— 训练截面数、样本数、标签缺失分布、标签截止日
  3. metrics    —— 样本外(walk-forward)的 IC / AUC / 胜率 / 分档
  4. features   —— 特征列名与顺序;顺序错了预测就是乱码

存 JSON 不存 pickle:产物要能被人打开看、能 diff、能跨 Python 版本读。
岭回归的全部状态就是四组数;LightGBM 用它自带的文本格式。

可用性三态
----------
前端 ``machine_learning.availability`` 只认三个值,与项目既有的
"缺失要说明原因"约定一致:

  - ``available``   : 有产物,且样本外指标达到门槛
  - ``pending``     : 有产物但没达标 / 样本太少,**不展示预测**
  - ``not_trained`` : 根本没训练过

``pending`` 与 ``not_trained`` 刻意分开:一个是"训了但不够好",
一个是"还没训"。合成一个状态,用户就不知道该等还是该动手。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import Model, load_model

# 产物格式版本。字段含义变更时 +1,加载端据此拒绝不认识的老产物。
SCHEMA_VERSION = 1

# 产物目录锚定 workbench 根(registry.py -> ml -> engine -> workbench),
# 不跟随进程 CWD。写成相对路径 "data/models" 时,从别的目录启动 uvicorn
# 会指到一个不存在的路径,于是"训好的模型"被静默报成"未训练"——
# 这条错误路径不抛异常、不打日志,只是让页面少显示一整块内容。
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "models"

# 达标门槛。低于此值的模型只登记、不启用——宁可显示"待训练",
# 也不要把一个没有区分度的模型摆到界面上当结论。
MIN_TRAIN_DAYS = 20        # 样本外评估至少覆盖的截面数
MIN_SAMPLES = 300          # 参与评估的样本数
MIN_IC = 0.02              # 样本外 IC 均值(排序相关性)下限

AVAILABILITY = ("available", "pending", "not_trained")


@dataclass
class Artifact:
    """一份已落盘的模型产物。"""

    name: str
    path: Path
    payload: Dict[str, Any]

    @property
    def backend(self) -> str:
        return str(self.payload.get("backend") or "unknown")

    @property
    def features(self) -> List[str]:
        return list(self.payload.get("features") or [])

    @property
    def metrics(self) -> Dict[str, Any]:
        return dict(self.payload.get("metrics") or {})

    @property
    def dataset(self) -> Dict[str, Any]:
        return dict(self.payload.get("dataset") or {})

    @property
    def horizon(self) -> str:
        return str(self.payload.get("horizon") or "")

    @property
    def trained_at(self) -> str:
        return str(self.payload.get("trained_at") or "")

    def load(self) -> Model:
        """还原可预测的模型对象。backend 不匹配当前环境时抛错。"""
        state = self.payload.get("state")
        if not isinstance(state, dict):
            raise ValueError(f"产物缺少 state 段: {self.path}")
        return load_model(state)

    def as_dict(self) -> dict:
        """给 API / 前端的摘要。不含权重——前端不需要,也没必要传。"""
        return {
            "name": self.name,
            "backend": self.backend,
            "horizon": self.horizon,
            "trained_at": self.trained_at,
            "n_features": len(self.features),
            "features": self.features,
            "dataset": self.dataset,
            "metrics": self.metrics,
        }


def artifact_dir(base: Optional[str | Path] = None) -> Path:
    return Path(base) if base else DEFAULT_DIR


def artifact_path(name: str, *, base: Optional[str | Path] = None) -> Path:
    safe = _safe_name(name)
    return artifact_dir(base) / f"{safe}.json"


def _safe_name(name: str) -> str:
    """产物名做白名单过滤:名字会拼进文件路径,不能带分隔符。"""
    cleaned = "".join(ch for ch in str(name) if ch.isalnum() or ch in ("-", "_"))
    if not cleaned:
        raise ValueError(f"非法产物名: {name!r}")
    return cleaned


def save_artifact(
    model: Model,
    *,
    name: str,
    horizon: str,
    features: List[str],
    metrics: Dict[str, Any],
    dataset: Dict[str, Any],
    folds: Optional[List[dict]] = None,
    params: Optional[Dict[str, Any]] = None,
    base: Optional[str | Path] = None,
) -> Path:
    """落盘一份产物。返回文件路径。

    features 的顺序必须与训练时喂给模型的列顺序一致,加载后据此重排,
    否则预测值是把 A 因子的权重套在 B 因子上,结果毫无意义却看不出错。
    """
    if not features:
        raise ValueError("features 不能为空:没有列名的产物无法安全复用")
    state = model.state_dict()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": _safe_name(name),
        "backend": getattr(model, "backend", "unknown"),
        "horizon": horizon,
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "features": list(features),
        "params": dict(params or {}),
        "dataset": dict(dataset or {}),
        "metrics": dict(metrics or {}),
        "folds": list(folds or []),
        "state": state,
    }

    path = artifact_path(name, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, payload)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再 rename:训练中途崩了也不会留下半个产物。

    半截 JSON 会让加载端报解析错,而不是报"没有模型",
    排查方向就被带偏了。
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_artifact(name: str, *, base: Optional[str | Path] = None) -> Optional[Artifact]:
    """读取产物。文件不存在返回 None(不是抛错——"没训练"是正常状态)。

    文件存在但坏了则抛错:那是真问题,不该被当成"还没训练"掩盖过去。
    """
    path = artifact_path(name, base=base)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    version = int(payload.get("schema_version") or 0)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"产物格式版本 {version} 高于当前支持的 {SCHEMA_VERSION},请升级代码"
        )
    return Artifact(name=str(payload.get("name") or name), path=path, payload=payload)


def list_artifacts(*, base: Optional[str | Path] = None) -> List[str]:
    directory = artifact_dir(base)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def evaluate_availability(artifact: Optional[Artifact]) -> Dict[str, Any]:
    """把产物翻译成前端要的三态 + 人话原因。

    门槛不达标时返回 pending 并说清差在哪一项,而不是笼统一句
    "模型不可用"。用户看到"样本外只有 12 个截面(需要 20)"
    才知道下一步是继续攒数据,而不是去调参。
    """
    if artifact is None:
        return {
            "availability": "not_trained",
            "reason": "尚未训练任何模型",
            "backend": None,
            "metrics": {},
        }

    metrics = artifact.metrics
    dataset = artifact.dataset
    n_days = _as_int(metrics.get("n_days")) or _as_int(dataset.get("replayed_days")) or 0
    n_samples = _as_int(metrics.get("n_samples")) or 0
    ic_mean = metrics.get("ic_mean")

    blockers: List[str] = []
    if n_days < MIN_TRAIN_DAYS:
        blockers.append(f"样本外仅 {n_days} 个截面(需要 {MIN_TRAIN_DAYS})")
    if n_samples < MIN_SAMPLES:
        blockers.append(f"样本外仅 {n_samples} 条样本(需要 {MIN_SAMPLES})")
    if ic_mean is None:
        # 样本太少导致 IC 算不出来,和"算出来是 0"是两件事。
        blockers.append("样本外 IC 无法计算(有效截面不足)")
    elif float(ic_mean) < MIN_IC:
        blockers.append(f"样本外 IC {float(ic_mean):.4f} 低于门槛 {MIN_IC}")

    if blockers:
        return {
            "availability": "pending",
            "reason": ";".join(blockers),
            "backend": artifact.backend,
            "trained_at": artifact.trained_at,
            "metrics": metrics,
        }

    return {
        "availability": "available",
        "reason": (
            f"{artifact.backend} 后端,{n_days} 个样本外截面,"
            f"IC {float(ic_mean):.4f}"
        ),
        "backend": artifact.backend,
        "trained_at": artifact.trained_at,
        "metrics": metrics,
    }


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def thresholds() -> Dict[str, Any]:
    """门槛值对外可见。前端能显示"差多少",而不是只说不达标。"""
    return {
        "min_train_days": MIN_TRAIN_DAYS,
        "min_samples": MIN_SAMPLES,
        "min_ic": MIN_IC,
    }
