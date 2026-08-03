"""模型后端:GBDT 优先,岭回归兜底。

为什么要兜底
------------
requirements.txt 里写了 lightgbm/scikit-learn,但当前运行环境**并没有装**。
如果直接 `import lightgbm`,整条机器学习链路在这台机器上就是不可用的死代码。

因此本模块提供两个后端,接口一致:

  - ``GBDTModel``  : LightGBM,装了才可用
  - ``RidgeModel`` : 纯 numpy 岭回归(闭式解),永远可用

选择结果必须如实记录在产物元数据的 ``backend`` 字段里。把降级后端讲成
"GBDT 模型"是欺骗性描述——排序能力差一截,却让人以为用了树模型。
前端显示 backend,用户看得见自己实际在用什么。

岭回归为什么够用作兜底
----------------------
本层任务是**排序**(哪些票未来更强),不是精确预测收益值。因子已在
normalize 层做过横截面 rank 归一化,线性模型在这种输入上是合理的基线;
IC/AUC 这些排序指标对单调变换不敏感。它给不出交互项,但能给出可信的
下限,并让整条链路(标签→切分→指标→产物)在任何环境下都能跑通、可测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence

import numpy as np


def lightgbm_available() -> bool:
    """LightGBM 是否可导入。不缓存结果:装包后无需重启进程。"""
    try:
        import lightgbm  # noqa: F401
    except Exception:
        return False
    return True


class Model(Protocol):
    """模型后端协议。fit 返回自身,predict 返回与输入等长的分数。"""

    backend: str

    def fit(self, x: np.ndarray, y: np.ndarray) -> "Model": ...

    def predict(self, x: np.ndarray) -> np.ndarray: ...

    def feature_importance(self, names: Sequence[str]) -> Dict[str, float]: ...


# ----------------------------------------------------------- 岭回归(兜底)

@dataclass
class RidgeModel:
    """标准化 + 闭式解岭回归。无第三方依赖。

    y = (x - mu) / sigma · w + b
    w = (XᵀX + λI)⁻¹ Xᵀy
    """

    alpha: float = 1.0
    backend: str = field(default="ridge_numpy", init=False)
    _mu: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _sigma: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _w: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _b: float = field(default=0.0, init=False, repr=False)

    def fit(self, x: np.ndarray, y: np.ndarray) -> "RidgeModel":
        x = np.asarray(x, dtype="float64")
        y = np.asarray(y, dtype="float64").ravel()
        if x.ndim != 2:
            raise ValueError("x 必须是二维矩阵")
        if len(x) != len(y):
            raise ValueError("x 与 y 行数不一致")
        if len(x) == 0:
            raise ValueError("训练集为空")

        # 缺失值用训练集列均值填充,并把均值记下来供 predict 复用。
        # 注意:填充统计量只能来自训练集,用全量算 mu 就是泄漏。
        self._mu = np.nanmean(x, axis=0)
        self._mu = np.where(np.isnan(self._mu), 0.0, self._mu)
        xf = np.where(np.isnan(x), self._mu, x)

        sigma = xf.std(axis=0)
        # 常数列(sigma=0)除法会产生 inf/nan:置 1 等价于把该列压成 0 贡献
        self._sigma = np.where(sigma < 1e-12, 1.0, sigma)
        xs = (xf - self._mu) / self._sigma

        n_features = xs.shape[1]
        gram = xs.T @ xs + self.alpha * np.eye(n_features)
        try:
            self._w = np.linalg.solve(gram, xs.T @ (y - y.mean()))
        except np.linalg.LinAlgError:
            # 极端共线时退到最小二乘伪逆,不让训练直接崩掉
            self._w = np.linalg.pinv(gram) @ (xs.T @ (y - y.mean()))
        self._b = float(y.mean())
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if self._w is None or self._mu is None or self._sigma is None:
            raise RuntimeError("模型尚未训练")
        x = np.asarray(x, dtype="float64")
        xf = np.where(np.isnan(x), self._mu, x)
        xs = (xf - self._mu) / self._sigma
        return xs @ self._w + self._b

    def feature_importance(self, names: Sequence[str]) -> Dict[str, float]:
        """标准化系数的绝对值。已标准化,故可跨特征直接比较量级。"""
        if self._w is None:
            return {}
        return {
            str(name): float(abs(weight))
            for name, weight in zip(names, self._w, strict=False)
        }

    def coefficients(self, names: Sequence[str]) -> Dict[str, float]:
        """带符号的系数——方向本身是信息(负号表示该因子越大未来越弱)。"""
        if self._w is None:
            return {}
        return {
            str(name): float(weight)
            for name, weight in zip(names, self._w, strict=False)
        }

    # 序列化:岭回归的全部状态就是四组数,纯 JSON 即可。
    # 刻意不用 pickle —— 产物要能被人读、被 diff、跨 Python 版本加载。
    def state_dict(self) -> dict:
        if self._w is None:
            raise RuntimeError("模型尚未训练,无状态可导出")
        return {
            "backend": self.backend,
            "alpha": float(self.alpha),
            "mu": [float(v) for v in self._mu],
            "sigma": [float(v) for v in self._sigma],
            "w": [float(v) for v in self._w],
            "b": float(self._b),
        }

    @classmethod
    def from_state(cls, state: dict) -> "RidgeModel":
        model = cls(alpha=float(state.get("alpha", 1.0)))
        model._mu = np.asarray(state["mu"], dtype="float64")
        model._sigma = np.asarray(state["sigma"], dtype="float64")
        model._w = np.asarray(state["w"], dtype="float64")
        model._b = float(state.get("b", 0.0))
        return model


# ----------------------------------------------------------- LightGBM

@dataclass
class GBDTModel:
    """LightGBM 回归。仅在 lightgbm 可导入时可用。"""

    n_estimators: int = 200
    learning_rate: float = 0.05
    num_leaves: int = 15
    min_child_samples: int = 30
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_lambda: float = 1.0
    random_state: int = 42
    backend: str = field(default="lightgbm", init=False)
    _model: object = field(default=None, init=False, repr=False)

    def fit(self, x: np.ndarray, y: np.ndarray) -> "GBDTModel":
        import lightgbm as lgb

        x = np.asarray(x, dtype="float64")
        y = np.asarray(y, dtype="float64").ravel()
        if len(x) == 0:
            raise ValueError("训练集为空")
        # 参数偏保守:金融截面数据信噪比低,深树几乎必然过拟合。
        # num_leaves 小、min_child_samples 大、加 L2,是刻意的欠拟合偏好。
        self._model = lgb.LGBMRegressor(
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            subsample_freq=1,
            colsample_bytree=self.colsample_bytree,
            reg_lambda=self.reg_lambda,
            random_state=self.random_state,
            verbose=-1,
        )
        self._model.fit(x, y)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("模型尚未训练")
        return np.asarray(self._model.predict(np.asarray(x, dtype="float64")))

    def feature_importance(self, names: Sequence[str]) -> Dict[str, float]:
        if self._model is None:
            return {}
        gains = getattr(self._model, "feature_importances_", None)
        if gains is None:
            return {}
        return {
            str(name): float(value)
            for name, value in zip(names, gains, strict=False)
        }

    def state_dict(self) -> dict:
        """LightGBM 用自带的文本格式序列化(不是 pickle,跨版本更稳)。"""
        if self._model is None:
            raise RuntimeError("模型尚未训练,无状态可导出")
        return {
            "backend": self.backend,
            "booster": self._model.booster_.model_to_string(),
        }

    @classmethod
    def from_state(cls, state: dict) -> "GBDTModel":
        import lightgbm as lgb

        model = cls()
        model._model = _BoosterWrapper(
            lgb.Booster(model_str=state["booster"])
        )
        return model


@dataclass
class _BoosterWrapper:
    """让原始 Booster 兼容 LGBMRegressor 的 predict/importance 接口。

    从字符串恢复得到的是 Booster 而非 sklearn wrapper,
    GBDTModel 的其余代码按 wrapper 写,这里补齐差异。
    """

    booster: object

    def predict(self, x):
        return self.booster.predict(x)

    @property
    def feature_importances_(self):
        return self.booster.feature_importance(importance_type="gain")


def make_model(prefer: str = "auto", **kwargs) -> Model:
    """按偏好构造模型后端。

    prefer:
      auto     - 有 lightgbm 用 lightgbm,否则岭回归
      lightgbm - 强制 GBDT;没装则抛错(而不是静默降级)
      ridge    - 强制岭回归

    "强制"分支刻意抛错:调用方明确要 GBDT 时,静默给个线性模型
    会让下游把结果当成树模型的结论。
    """
    prefer = (prefer or "auto").lower()
    if prefer == "ridge":
        return RidgeModel(**{k: v for k, v in kwargs.items() if k == "alpha"})
    if prefer == "lightgbm":
        if not lightgbm_available():
            raise RuntimeError(
                "指定 backend=lightgbm 但当前环境未安装 lightgbm;"
                "请 pip install lightgbm,或改用 backend=auto/ridge"
            )
        return GBDTModel(**{k: v for k, v in kwargs.items() if k in _GBDT_KEYS})
    if prefer != "auto":
        raise ValueError(f"未知 backend: {prefer}")
    if lightgbm_available():
        return GBDTModel(**{k: v for k, v in kwargs.items() if k in _GBDT_KEYS})
    return RidgeModel(**{k: v for k, v in kwargs.items() if k == "alpha"})


def load_model(state: dict) -> Model:
    """按 state["backend"] 还原模型。

    产物里记的是**训练时实际用的**后端。如果产物是 lightgbm 训的、
    但当前环境没装 lightgbm,这里抛错而不是拿岭回归凑数——
    用另一个模型冒充产物,评估指标就全部对不上了。
    """
    backend = str((state or {}).get("backend") or "")
    if backend == "ridge_numpy":
        return RidgeModel.from_state(state)
    if backend == "lightgbm":
        if not lightgbm_available():
            raise RuntimeError(
                "该产物由 lightgbm 训练,但当前环境未安装 lightgbm,无法加载;"
                "请 pip install lightgbm,或用 ridge 后端重新训练"
            )
        return GBDTModel.from_state(state)
    raise ValueError(f"产物 backend 无法识别: {backend!r}")


_GBDT_KEYS = frozenset({
    "n_estimators", "learning_rate", "num_leaves", "min_child_samples",
    "subsample", "colsample_bytree", "reg_lambda", "random_state",
})
