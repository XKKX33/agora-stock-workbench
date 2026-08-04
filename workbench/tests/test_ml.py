"""机器学习层单测:标签 / 切分 / 指标 / 模型 / 产物 / 训练。

全部用合成数据,**不碰数据库**——engine/ml 下除 dataset.py 外都刻意做成
无依赖模块,正是为了能这样测。dataset.py 需要真实 Store,由离线扫描
用例间接覆盖,这里不重复搭一套假库。

重点覆盖"容易错且错了看不出来"的地方:
- 标签缺失原因分类(把"要等"和"要修"混成一个数字,就没人去修)
- purge 宽度(purge 少一天,指标偏乐观,而且不会报错)
- 算不出的指标返回 None 而不是 0 / 0.5 / inf
- 产物序列化后预测值必须逐位一致(权重错位不抛错,只会静默胡说)
- backend 不匹配时拒绝加载,而不是拿岭回归冒充

运行:
    set PYTHONHOME=
    python workbench/tests/test_ml.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

_ENGINE_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ENGINE_PARENT not in sys.path:
    sys.path.insert(0, _ENGINE_PARENT)

from engine.ml import metrics as M  # noqa: E402
from engine.ml import model as MODEL  # noqa: E402
from engine.ml import registry as REG  # noqa: E402
from engine.ml.labels import (  # noqa: E402
    CloseLookup, TradingCalendar, build_labels, to_binary,
)
from engine.ml.splits import (  # noqa: E402
    Fold, assert_no_leakage, purged_walk_forward, split_frame,
)
from engine.ml.train import MIN_FOLD_SAMPLES, predict_frame, train_on_frame  # noqa: E402


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ok: {name}")


# ---------------------------------------------------------------- 合成数据

# 60 个合成"交易日"。刻意不含周末概念——日历由 TradingCalendar 显式给定,
# 正是为了让"第 N 个交易日"的定义与真实日期脱钩、可单测。
_DAYS = [f"2025{m:02d}{d:02d}" for m in (1, 2, 3) for d in range(1, 21)]


def _daily_frame(codes, days, *, drop=()):
    """构造 (ts_code, trade_date, close) 长表。drop 中的组合刻意缺失。"""
    rows = []
    drop = set(drop)
    for i, code in enumerate(codes):
        for j, day in enumerate(days):
            if (code, day) in drop:
                continue
            rows.append({"ts_code": code, "trade_date": day,
                         "close": 10.0 + i + j * 0.1})
    return pd.DataFrame(rows)


def _synthetic_samples(n_days=60, n_stocks=30, *, seed=7, signal=0.6):
    """带真实信号的样本表:label 由 f_signal 线性驱动 + 噪声。

    f_noise 与标签无关,f_flat 是常数列(考验 sigma=0 的除零保护)。
    训练完 f_signal 的重要性应明显最高,否则说明列顺序在某处错位了。
    """
    rng = np.random.default_rng(seed)
    days = _DAYS[:n_days]
    rows = []
    for day in days:
        f_signal = rng.normal(size=n_stocks)
        f_noise = rng.normal(size=n_stocks)
        label = signal * f_signal + 0.4 * rng.normal(size=n_stocks)
        for k in range(n_stocks):
            rows.append({
                "ts_code": f"{k:06d}.SZ",
                "as_of": day,
                "f_signal": float(f_signal[k]),
                "f_noise": float(f_noise[k]),
                "f_flat": 1.0,
                "label": float(label[k]),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 标签

def test_labels_horizon_and_missing_reasons():
    days = _DAYS[:10]
    codes = ["000001.SZ", "000002.SZ"]
    # 000002 在第 6 个交易日停牌(它的 T+5 目标日无行情)
    daily = _daily_frame(codes, days, drop=(("000002.SZ", days[5]),))
    # 日历比行情多覆盖 4 天未来(线上 ingest 就是这么拉的):
    # 这样"目标日还没走到"和"日历该回补了"才是两个可分辨的状态。
    cal = TradingCalendar(_DAYS[:14])
    closes = CloseLookup(daily)

    samples = pd.DataFrame([
        {"ts_code": "000001.SZ", "as_of": days[0]},   # 正常:T+5 = days[5]
        {"ts_code": "000002.SZ", "as_of": days[0]},   # 目标日停牌
        {"ts_code": "000001.SZ", "as_of": days[6]},   # T+5=第12天,行情还没到
        {"ts_code": "000001.SZ", "as_of": days[9]},   # T+5 超出日历末日 -> 报修
        {"ts_code": "999999.SZ", "as_of": days[0]},   # 基准日无收盘价
    ])
    labels, report = build_labels(samples, calendar=cal, closes=closes, horizon="ret5")

    expect = (10.0 + 5 * 0.1) / 10.0 - 1.0
    _check("正常样本标签 = T+5收盘/基准-1", abs(labels.iloc[0] - expect) < 1e-12)
    _check("停牌样本标签为 NaN", pd.isna(labels.iloc[1]))
    _check("未来未到样本为 NaN", pd.isna(labels.iloc[2]))
    _check("resolved 只数可算出的", report.resolved == 1)
    _check("目标日停牌归类 target_bar_missing",
           report.missing.get("target_bar_missing") == 1)
    _check("未来未到归类 future_not_reached",
           report.missing.get("future_not_reached") == 1)
    _check("日历末日归类 calendar_missing",
           report.missing.get("calendar_missing") == 1)
    _check("基准缺失归类 base_missing", report.missing.get("base_missing") == 1)
    _check("needs_attention 剔除 future_not_reached",
           "future_not_reached" not in report.needs_attention())
    _check("needs_attention 保留三类要人处理的", len(report.needs_attention()) == 3)


def test_labels_use_market_calendar_not_next_bar():
    """停牌股的 T+5 必须按市场日历定位,不能顺延到下一根可用K线。

    这是标签口径最致命的错误:顺延会把几个月的收益当成 5 日收益。
    """
    days = _DAYS[:12]
    # 000003 在 days[1..9] 全部停牌,只有 days[0] 与 days[10] 有行情。
    # 顺延实现会把 days[10] 的收益当标签(错);
    # 按市场日历取 T+5 = days[5],该日无行情 -> 标签缺失(对)。
    drop = {("000003.SZ", d) for d in days[1:10]}
    daily = _daily_frame(["000003.SZ"], days, drop=drop)
    samples = pd.DataFrame([{"ts_code": "000003.SZ", "as_of": days[0]}])
    labels, report = build_labels(
        samples, calendar=TradingCalendar(days),
        closes=CloseLookup(daily), horizon="ret5",
    )
    _check("停牌期跨越 T+N 时标签缺失而非顺延", pd.isna(labels.iloc[0]))
    _check("缺失原因是目标日无行情", report.missing.get("target_bar_missing") == 1)


def test_calendar_sessions_after_non_trading_day():
    cal = TradingCalendar(["20250106", "20250107", "20250108", "20250109"])
    _check("交易日内 T+1 正确", cal.sessions_after("20250106", 1) == "20250107")
    _check("交易日内 T+3 正确", cal.sessions_after("20250106", 3) == "20250109")
    # 20250105 是周末,不在日历内:第 1 个开市日应是首个 > 它的日子
    _check("非交易日起算 T+1 取首个开市日",
           cal.sessions_after("20250105", 1) == "20250106")
    _check("日历不够长返回 None", cal.sessions_after("20250109", 1) is None)
    _check("max_day 正确", cal.max_day == "20250109")
    try:
        cal.sessions_after("20250106", 0)
    except ValueError:
        _check("n<=0 抛 ValueError", True)
    else:
        _check("n<=0 抛 ValueError", False)


def test_to_binary_keeps_nan():
    binary = to_binary(pd.Series([0.05, -0.02, float("nan"), 0.0]))
    _check("正收益 -> 1", binary.iloc[0] == 1.0)
    _check("负收益 -> 0", binary.iloc[1] == 0.0)
    _check("NaN 保持 NaN(不当成负例)", pd.isna(binary.iloc[2]))
    _check("零收益不算赢", binary.iloc[3] == 0.0)


# ---------------------------------------------------------------- 切分

def test_purged_walk_forward_gap_and_order():
    days = _DAYS[:60]
    folds = purged_walk_forward(days, horizon_days=5, n_splits=3, min_train_days=20)
    _check("产出 3 折", len(folds) == 3)
    _check("折号按时间升序重排", [f.index for f in folds] == [0, 1, 2])
    _check("折之间时间递增", folds[0].test_end < folds[1].test_start)
    for fold in folds:
        _check(f"第{fold.index}折 purge 宽度 = horizon(5)", len(fold.purged_days) == 5)
        _check(f"第{fold.index}折训练期完全早于测试期", fold.train_end < fold.test_start)
        # 守门断言:训练末日的标签窗口不得触及测试首日
        assert_no_leakage(fold, 5, days)
    _check("最后一折贴着最新数据", folds[-1].test_end == days[-1])


def test_embargo_widens_gap():
    days = _DAYS[:60]
    plain = purged_walk_forward(days, horizon_days=5, n_splits=2, min_train_days=20)
    embargoed = purged_walk_forward(
        days, horizon_days=5, n_splits=2, min_train_days=20, embargo_days=10
    )
    _check("embargo 后 purge 宽度 = horizon + embargo",
           all(len(f.purged_days) == 15 for f in embargoed))
    _check("embargo 缩短训练集", embargoed[-1].train_end < plain[-1].train_end)


def test_insufficient_days_yields_no_folds():
    """天数不够就少给折,不硬凑——硬凑出来的折训练集只有几天。"""
    folds = purged_walk_forward(_DAYS[:20], horizon_days=5, n_splits=3,
                                min_train_days=20)
    _check("天数不足时返回空折列表", folds == [])


def test_assert_no_leakage_catches_bad_purge():
    days = _DAYS[:30]
    # 手工造一折:训练末日 days[9],测试首日 days[12],purge 只有 2 天,
    # 而 horizon=5 -> 训练末日的标签伸到 days[14],已越过测试首日。
    bad = Fold(index=0, train_days=tuple(days[:10]),
               test_days=tuple(days[12:20]), purged_days=tuple(days[10:12]))
    try:
        assert_no_leakage(bad, 5, days)
    except AssertionError:
        _check("purge 不足时守门断言抛错", True)
    else:
        _check("purge 不足时守门断言抛错", False)


def test_split_frame_keeps_whole_days():
    samples = _synthetic_samples(n_days=60, n_stocks=10)
    days = sorted(samples["as_of"].unique())
    fold = purged_walk_forward(days, horizon_days=5, n_splits=2,
                               min_train_days=20)[0]
    train, test = split_frame(samples, fold)
    _check("训练测试无交集", set(train["as_of"]).isdisjoint(set(test["as_of"])))
    _check("同日样本不被拆开",
           all(int((train["as_of"] == d).sum()) == 10 for d in set(train["as_of"])))
    _check("purge 日既不在训练也不在测试",
           set(fold.purged_days).isdisjoint(set(train["as_of"]) | set(test["as_of"])))


# ---------------------------------------------------------------- 指标

def test_metrics_return_none_when_uncomputable():
    _check("样本<3 的相关系数为 None",
           M.correlation(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0])) is None)
    _check("常数列相关系数为 None",
           M.correlation(pd.Series([1.0, 1.0, 1.0, 1.0]),
                         pd.Series([1.0, 2.0, 3.0, 4.0])) is None)
    _check("单一类别 AUC 为 None,不是 0.5",
           M.auc(pd.Series([0.1, 0.2, 0.3]), pd.Series([1.0, 1.0, 1.0])) is None)
    _check("无亏损样本盈亏比为 None,不是 inf",
           M.profit_factor(pd.Series([0.01, 0.02, 0.03])) is None)
    _check("空样本胜率为 None", M.hit_rate(pd.Series([], dtype="float64")) is None)
    _check("空表 top-k 收益为 None", M.top_bucket_return(pd.DataFrame()) is None)
    _check("空表 IC 三元组全空", M.cross_section_ic(pd.DataFrame()) == (None, None, []))


def test_metrics_values_are_correct():
    _check("完全同序 spearman = 1",
           abs(M.correlation(pd.Series([1.0, 2.0, 3.0, 4.0]),
                             pd.Series([10.0, 20.0, 30.0, 40.0]),
                             method="spearman") - 1.0) < 1e-9)
    _check("完全反序 spearman = -1",
           abs(M.correlation(pd.Series([1.0, 2.0, 3.0, 4.0]),
                             pd.Series([40.0, 30.0, 20.0, 10.0]),
                             method="spearman") + 1.0) < 1e-9)
    _check("完全可分 AUC = 1",
           abs(M.auc(pd.Series([0.9, 0.8, 0.2, 0.1]),
                     pd.Series([1.0, 1.0, 0.0, 0.0])) - 1.0) < 1e-9)
    _check("完全反分 AUC = 0",
           abs(M.auc(pd.Series([0.1, 0.2, 0.8, 0.9]),
                     pd.Series([1.0, 1.0, 0.0, 0.0]))) < 1e-9)
    _check("胜率 = 2/4",
           abs(M.hit_rate(pd.Series([0.1, 0.2, -0.1, -0.3])) - 0.5) < 1e-9)
    _check("盈亏比 = 0.3/0.4",
           abs(M.profit_factor(pd.Series([0.1, 0.2, -0.1, -0.3])) - 0.75) < 1e-9)


def test_cross_section_ic_is_per_day():
    """逐日算再平均:不能把多日样本混起来算一个大相关系数。

    两天各自日内 pred 与 label 完全同序(日内 IC = 1),但两天的收益
    水平相差两个量级。混算会被"日间涨跌差异"带偏,逐日算稳定得 1.0。
    """
    rows = []
    for day, scale in (("20250101", 0.001), ("20250102", 0.100)):
        for k in range(5):
            rows.append({"as_of": day, "pred": float(k), "label": scale * (k + 1)})
    ic_mean, ic_ir, daily = M.cross_section_ic(pd.DataFrame(rows))
    _check("逐日 IC 均值 = 1.0", abs(ic_mean - 1.0) < 1e-9)
    _check("每日明细各 5 个样本", [d["n"] for d in daily] == [5, 5])
    # 这里 IR 为 None 是因为只有两天(不够 MIN_DAYS_FOR_IC_IR),
    # 不是因为 IC 无波动——两个原因都会给 None,别把它当成后者的证据。
    _check("两天算不出 IR", ic_ir is None)


def test_ic_ir_needs_enough_days_not_enough_samples():
    """天数不够就不给 IC IR,哪怕每天的横截面很宽。

    这条钉的是一个真实缺陷:旧实现只要 >= 2 天就给值,而 IR 的分母是
    IC 的**跨日**标准差,两三天算出来的标准差偏小,IR 会虚高到 4~5
    ——真实股票因子的 IC IR 大致在 0.5 量级,4.8 只可能是天数太少的假象。
    样本数再多也补不了天数:分母数的是天,不是行。
    """
    def _frame(n_days: int) -> pd.DataFrame:
        # 固定种子:逐日加不同噪声,让**日 IC 本身有波动**(算得出非零标准差)。
        # 若日内完全同序,每天 IC 都是 1.0、标准差为 0,那样 IR 为 None 是
        # "无波动"而不是"天数不够",就钉不住这条规则了。
        rng = np.random.RandomState(11)
        rows = []
        for day in range(n_days):
            for k in range(30):
                rows.append({
                    "as_of": f"202501{day + 1:02d}",
                    "pred": float(k),
                    "label": 0.01 * k + rng.normal(0.0, 0.08),
                })
        return pd.DataFrame(rows)

    three = M.cross_section_ic(_frame(3))
    four = M.cross_section_ic(_frame(4))

    _check("3 天(90 行)不给 IR", three[1] is None)
    _check("3 天仍给 IC 均值", three[0] is not None)
    _check("4 天给 IR", four[1] is not None)
    # 日 IC 确实有波动,否则上一条是"无波动"通过的,不是"天数够"通过的
    ic_values = [d["ic"] for d in four[2] if d["ic"] is not None]
    _check("四天的日 IC 不全相等", len(set(ic_values)) > 1)
    # 分母是样本标准差(ddof=1)。ddof=0 会把分母算小、IR 算大,
    # 偏差方向恰好是"看起来更好"的那一侧,所以要逐位钉住。
    expected = float(np.mean(ic_values) / np.std(ic_values, ddof=1))
    _check("IR 用 ddof=1 的标准差", abs(four[1] - expected) < 1e-6)

    _, ir_short, daily_short = M.cross_section_ic(_frame(M.MIN_DAYS_FOR_IC_IR - 1))
    _check("差一天就不给 IR", ir_short is None)
    _check("但每日明细照给(不给值不等于不展示)",
           len(daily_short) == M.MIN_DAYS_FOR_IC_IR - 1)

    _, ir_ok, _ = M.cross_section_ic(_frame(M.MIN_DAYS_FOR_IC_IR))
    _check("够天数就给 IR", ir_ok is not None)


def test_evaluate_predictions_excludes_nan_labels():
    rows = [{"as_of": "20250101", "ts_code": str(k), "pred": float(k),
             "label": 0.01 * k} for k in range(10)]
    # 预测分最高但标签未知(未来没到):不该被当成 0 收益混进指标
    rows.append({"as_of": "20250101", "ts_code": "x", "pred": 9.9,
                 "label": float("nan")})
    result = M.evaluate_predictions(pd.DataFrame(rows), top_k=3)
    _check("NaN 标签样本被排除", result.n_samples == 10)
    _check("单日横截面 n_days = 1", result.n_days == 1)
    _check("完全同序时 IC = 1", abs(result.ic_mean - 1.0) < 1e-9)
    _check("top3 收益 = (0.09+0.08+0.07)/3",
           abs(result.top_bucket_return - 0.08) < 1e-9)
    payload = result.as_dict()
    _check("as_dict 带 monotonic 字段", "monotonic" in payload)
    _check("as_dict 带逐日 IC 明细", len(payload["daily_ic"]) == 1)


def test_decile_returns_monotonicity():
    rows = []
    for day in ("20250101", "20250102"):
        for k in range(20):
            rows.append({"as_of": day, "pred": float(k), "label": 0.01 * k})
    buckets = M.decile_returns(pd.DataFrame(rows), n_buckets=5)
    _check("产出 5 个桶", len(buckets) == 5)
    _check("桶1 是预测分最高档", buckets[0]["bucket"] == 1)
    _check("桶1 收益高于桶5", buckets[0]["avg_return"] > buckets[-1]["avg_return"])
    _check("单调递减判定为 True", M.is_monotonic_decreasing(buckets) is True)
    _check("有效桶<2 时单调性为 None",
           M.is_monotonic_decreasing([{"avg_return": None}]) is None)


# ---------------------------------------------------------------- 模型

def test_ridge_recovers_signal_and_handles_constant_column():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(500, 3))
    x[:, 2] = 1.0  # 常数列:sigma=0,除法必须被保护
    y = 2.0 * x[:, 0] - 1.0 * x[:, 1] + 0.1 * rng.normal(size=500)
    fitted = MODEL.RidgeModel(alpha=1.0).fit(x, y)
    coef = fitted.coefficients(["a", "b", "flat"])
    _check("正相关特征系数为正", coef["a"] > 0)
    _check("负相关特征系数为负", coef["b"] < 0)
    _check("常数列系数为 0(未产生 inf/nan)", abs(coef["flat"]) < 1e-9)
    _check("预测值全部有限", np.isfinite(fitted.predict(x)).all())
    importance = fitted.feature_importance(["a", "b", "flat"])
    _check("重要性取绝对值", importance["b"] > 0)
    _check("重要性排序 a > b > flat",
           importance["a"] > importance["b"] > importance["flat"])


def test_ridge_fills_nan_with_train_mean_only():
    """缺失值用**训练集**列均值填充。用全量算均值就是泄漏。"""
    x = np.array([[1.0, 5.0], [2.0, 6.0], [3.0, 7.0], [4.0, 8.0]])
    y = np.array([1.0, 2.0, 3.0, 4.0])
    fitted = MODEL.RidgeModel().fit(x, y)
    with_nan = fitted.predict(np.array([[float("nan"), 6.5]]))
    filled = fitted.predict(np.array([[2.5, 6.5]]))  # 第一列训练均值 = 2.5
    _check("NaN 用训练均值填充后预测一致",
           abs(float(with_nan[0]) - float(filled[0])) < 1e-12)


def test_ridge_predict_before_fit_raises():
    try:
        MODEL.RidgeModel().predict(np.zeros((2, 2)))
    except RuntimeError:
        _check("未训练即预测抛 RuntimeError", True)
    else:
        _check("未训练即预测抛 RuntimeError", False)
    try:
        MODEL.RidgeModel().state_dict()
    except RuntimeError:
        _check("未训练即导出状态抛 RuntimeError", True)
    else:
        _check("未训练即导出状态抛 RuntimeError", False)


def test_model_state_round_trip_is_bitwise_identical():
    """序列化必须逐位还原。权重错位不抛错,只会静默给出错的分数。"""
    rng = np.random.default_rng(11)
    x = rng.normal(size=(200, 4))
    y = x @ np.array([1.0, -2.0, 0.5, 0.0]) + 0.1 * rng.normal(size=200)
    fitted = MODEL.RidgeModel(alpha=2.0).fit(x, y)
    state = fitted.state_dict()
    # 真走一遍 JSON:确认没有 numpy 标量漏出去(json.dump 会直接报错)
    revived = MODEL.load_model(json.loads(json.dumps(state)))
    diff = float(np.abs(fitted.predict(x) - revived.predict(x)).max())
    _check("JSON 往返后预测完全一致", diff == 0.0)
    _check("backend 记录为 ridge_numpy", state["backend"] == "ridge_numpy")
    _check("alpha 被保留", abs(revived.alpha - 2.0) < 1e-12)


def test_load_model_refuses_unknown_and_missing_backend():
    for state, label in (({"backend": "xgboost"}, "不认识的 backend"),
                         ({}, "缺 backend"),
                         (None, "state 为 None")):
        try:
            MODEL.load_model(state)
        except ValueError:
            _check(f"{label} 抛 ValueError", True)
        else:
            _check(f"{label} 抛 ValueError", False)


def test_make_model_does_not_silently_degrade():
    """强制 lightgbm 但环境没装时必须抛错,不能给个线性模型冒充树模型。"""
    if MODEL.lightgbm_available():
        _check("装了 lightgbm 时 backend 为 lightgbm",
               MODEL.make_model("lightgbm").backend == "lightgbm")
    else:
        try:
            MODEL.make_model("lightgbm")
        except RuntimeError:
            _check("未装 lightgbm 时强制指定抛 RuntimeError", True)
        else:
            _check("未装 lightgbm 时强制指定抛 RuntimeError", False)
        _check("auto 在无 lightgbm 时回退岭回归",
               MODEL.make_model("auto").backend == "ridge_numpy")
        try:
            MODEL.load_model({"backend": "lightgbm", "booster": "x"})
        except RuntimeError:
            _check("未装 lightgbm 时拒绝加载 GBDT 产物(不拿岭回归凑数)", True)
        else:
            _check("未装 lightgbm 时拒绝加载 GBDT 产物(不拿岭回归凑数)", False)
    _check("ridge 显式指定生效", MODEL.make_model("ridge").backend == "ridge_numpy")
    try:
        MODEL.make_model("magic")
    except ValueError:
        _check("未知 backend 抛 ValueError", True)
    else:
        _check("未知 backend 抛 ValueError", False)


# ---------------------------------------------------------------- 产物

def _fitted_model():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(300, 2))
    return MODEL.RidgeModel().fit(x, x @ np.array([1.0, -0.5]))


def test_artifact_save_load_round_trip():
    with tempfile.TemporaryDirectory(prefix="ml-artifact-") as tmp:
        path = REG.save_artifact(
            _fitted_model(), name="unit_test", horizon="ret5",
            features=["f_a", "f_b"],
            metrics={"ic_mean": 0.05, "n_days": 30, "n_samples": 900},
            dataset={"replayed_days": 30}, base=tmp,
        )
        _check("产物文件已落盘", os.path.exists(path))
        artifact = REG.load_artifact("unit_test", base=tmp)
        _check("产物可读回", artifact is not None)
        _check("特征顺序被保留", artifact.features == ["f_a", "f_b"])
        _check("backend 被记录", artifact.backend == "ridge_numpy")
        _check("horizon 被记录", artifact.horizon == "ret5")
        _check("trained_at 非空", len(artifact.trained_at) > 0)
        _check("dataset 段被保留", artifact.dataset["replayed_days"] == 30)
        _check("模型可从产物还原",
               np.isfinite(artifact.load().predict(np.zeros((1, 2)))).all())
        _check("as_dict 不外泄权重", "state" not in artifact.as_dict())
        _check("list_artifacts 列出名字", REG.list_artifacts(base=tmp) == ["unit_test"])
        _check("不存在的产物返回 None(不抛错)",
               REG.load_artifact("nope", base=tmp) is None)


def test_artifact_rejects_empty_features_and_sanitizes_name():
    with tempfile.TemporaryDirectory(prefix="ml-artifact-") as tmp:
        try:
            REG.save_artifact(_fitted_model(), name="x", horizon="ret5",
                              features=[], metrics={}, dataset={}, base=tmp)
        except ValueError:
            _check("空特征列表被拒绝", True)
        else:
            _check("空特征列表被拒绝", False)

        # 产物名会拼进文件路径:分隔符与点号必须被清掉,不能逃出产物目录
        escaped = REG.artifact_path("../../etc/passwd", base=tmp)
        _check("路径穿越字符被清除,产物仍落在目录内",
               os.path.dirname(os.path.abspath(escaped)) == os.path.abspath(tmp))
        try:
            REG.artifact_path("../..", base=tmp)
        except ValueError:
            _check("清理后为空的产物名被拒绝", True)
        else:
            _check("清理后为空的产物名被拒绝", False)


def test_artifact_rejects_future_schema_version():
    """比当前代码更新的产物格式必须拒绝,而不是按老字段瞎解释。"""
    with tempfile.TemporaryDirectory(prefix="ml-artifact-") as tmp:
        REG.save_artifact(_fitted_model(), name="future", horizon="ret5",
                          features=["a", "b"], metrics={}, dataset={}, base=tmp)
        path = REG.artifact_path("future", base=tmp)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = REG.SCHEMA_VERSION + 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            REG.load_artifact("future", base=tmp)
        except ValueError:
            _check("更高版本产物被拒绝加载", True)
        else:
            _check("更高版本产物被拒绝加载", False)


def test_availability_three_states():
    _check("无产物 -> not_trained",
           REG.evaluate_availability(None)["availability"] == "not_trained")

    with tempfile.TemporaryDirectory(prefix="ml-artifact-") as tmp:
        REG.save_artifact(
            _fitted_model(), name="good", horizon="ret5", features=["a", "b"],
            metrics={"ic_mean": 0.05, "n_days": REG.MIN_TRAIN_DAYS,
                     "n_samples": REG.MIN_SAMPLES},
            dataset={}, base=tmp,
        )
        state = REG.evaluate_availability(REG.load_artifact("good", base=tmp))
        _check("达标 -> available", state["availability"] == "available")
        _check("available 原因里带 IC 数值", "0.05" in state["reason"])

        # 两项同时不达标:原因要把两条都说清,用户才知道该攒数据还是该调参
        REG.save_artifact(
            _fitted_model(), name="weak", horizon="ret5", features=["a", "b"],
            metrics={"ic_mean": -0.15, "n_days": 15, "n_samples": 400},
            dataset={}, base=tmp,
        )
        state = REG.evaluate_availability(REG.load_artifact("weak", base=tmp))
        _check("不达标 -> pending", state["availability"] == "pending")
        _check("原因说明截面不足", "截面" in state["reason"])
        _check("原因说明 IC 低于门槛", "门槛" in state["reason"])

        # IC 算不出 ≠ IC 等于 0
        REG.save_artifact(
            _fitted_model(), name="nullic", horizon="ret5", features=["a", "b"],
            metrics={"ic_mean": None, "n_days": 30, "n_samples": 900},
            dataset={}, base=tmp,
        )
        state = REG.evaluate_availability(REG.load_artifact("nullic", base=tmp))
        _check("IC 为 None -> pending", state["availability"] == "pending")
        _check("原因写明无法计算而非 IC=0", "无法计算" in state["reason"])

    _check("门槛值对外可见",
           set(REG.thresholds()) == {"min_train_days", "min_samples", "min_ic"})


# ---------------------------------------------------------------- 训练

def test_train_on_frame_finds_real_signal():
    samples = _synthetic_samples(n_days=60, n_stocks=30, seed=7, signal=0.6)
    model, report = train_on_frame(samples, horizon="ret5", backend="ridge",
                                  n_splits=3, min_train_days=20, top_k=5)
    _check("训练产出最终模型", model is not None)
    _check("走满 3 折", report.n_folds == 3)
    _check("无折被跳过", report.skipped_folds == {})
    _check("特征列按名排序且不含元数据列",
           report.features == ["f_flat", "f_noise", "f_signal"])
    _check("样本外指标已算出",
           report.oos is not None and report.oos.ic_mean is not None)
    _check("有信号时样本外 IC 显著为正", report.oos.ic_mean > 0.2)
    _check("样本外 AUC > 0.5", report.oos.auc > 0.5)
    _check("样本外分层收益单调递减", report.oos.as_dict()["monotonic"] is True)
    _check("每折都有指标明细", len(report.fold_metrics) == 3)
    _check("折明细带训练/测试边界",
           all("train_end" in f and "test_start" in f for f in report.fold_metrics))
    payload = report.as_dict()
    _check("报告带过拟合缺口", payload["overfit_gap"] is not None)
    _check("有信号时过拟合缺口很小", abs(payload["overfit_gap"]) < 0.2)
    importance = model.feature_importance(report.features)
    _check("f_signal 重要性最高",
           max(importance, key=lambda k: importance[k]) == "f_signal")


def test_train_on_frame_reports_no_edge_when_none_exists():
    """纯噪声数据的样本外 IC 必须接近 0——不能靠拟合造出"优势"。"""
    samples = _synthetic_samples(n_days=60, n_stocks=30, seed=13, signal=0.0)
    _, report = train_on_frame(samples, horizon="ret5", backend="ridge",
                               n_splits=3, min_train_days=20)
    _check("噪声数据仍产出样本外指标", report.oos.ic_mean is not None)
    _check("噪声数据样本外 IC 接近 0", abs(report.oos.ic_mean) < 0.1)


def test_train_on_frame_degrades_honestly():
    empty = pd.DataFrame(columns=["ts_code", "as_of", "label", "f_a"])
    model, report = train_on_frame(empty, horizon="ret5", backend="ridge")
    _check("空样本表不返回模型", model is None)
    _check("空样本表记录 empty_dataset", "empty_dataset" in report.skipped_folds)

    unlabeled = _synthetic_samples(n_days=60, n_stocks=30)
    unlabeled["label"] = float("nan")
    model, report = train_on_frame(unlabeled, horizon="ret5", backend="ridge")
    _check("全无标签不返回模型", model is None)
    _check("全无标签记录 no_labeled_samples",
           "no_labeled_samples" in report.skipped_folds)

    short = _synthetic_samples(n_days=20, n_stocks=30)
    model, report = train_on_frame(short, horizon="ret5", backend="ridge",
                                   n_splits=3, min_train_days=20)
    _check("天数不足不返回模型", model is None)
    _check("天数不足记录 insufficient_days",
           "insufficient_days" in report.skipped_folds)

    # 每天只有 2 只票 -> 折内训练样本不够,跳过该折而不是拿噪声硬拟合
    thin = _synthetic_samples(n_days=60, n_stocks=2)
    model, report = train_on_frame(thin, horizon="ret5", backend="ridge",
                                   n_splits=3, min_train_days=20)
    _check(f"折内训练样本<{MIN_FOLD_SAMPLES} 时跳过",
           report.skipped_folds.get("train_too_small", 0) > 0)
    _check("所有折都跳过时不返回模型", model is None)

    try:
        train_on_frame(_synthetic_samples(n_days=30), horizon="ret7")
    except ValueError:
        _check("未知期限抛 ValueError", True)
    else:
        _check("未知期限抛 ValueError", False)


def test_predict_frame_reports_missing_features():
    """缺列如实上报,不静默补 0——归一化口径下补 0 等于"该因子最差"。"""
    samples = _synthetic_samples(n_days=60, n_stocks=30, seed=7)
    model, report = train_on_frame(samples, horizon="ret5", backend="ridge",
                                   n_splits=3, min_train_days=20)
    with tempfile.TemporaryDirectory(prefix="ml-artifact-") as tmp:
        REG.save_artifact(model, name="pred_test", horizon="ret5",
                          features=report.features,
                          metrics=report.oos.as_dict(), dataset={}, base=tmp)
        artifact = REG.load_artifact("pred_test", base=tmp)

        latest = samples[samples["as_of"] == samples["as_of"].max()]
        preds, missing = predict_frame(artifact, latest)
        _check("完整特征时无缺列", missing == [])
        _check("预测长度与输入一致", len(preds) == len(latest))
        _check("预测无 NaN", preds.notna().all())

        preds2, missing2 = predict_frame(artifact, latest.drop(columns=["f_noise"]))
        _check("缺列被如实上报", missing2 == ["f_noise"])
        _check("缺列改变了预测(说明没有当成 0 混过去)",
               not np.allclose(preds.to_numpy(), preds2.to_numpy()))


_TESTS = (
    test_labels_horizon_and_missing_reasons,
    test_labels_use_market_calendar_not_next_bar,
    test_calendar_sessions_after_non_trading_day,
    test_to_binary_keeps_nan,
    test_purged_walk_forward_gap_and_order,
    test_embargo_widens_gap,
    test_insufficient_days_yields_no_folds,
    test_assert_no_leakage_catches_bad_purge,
    test_split_frame_keeps_whole_days,
    test_metrics_return_none_when_uncomputable,
    test_metrics_values_are_correct,
    test_cross_section_ic_is_per_day,
    test_evaluate_predictions_excludes_nan_labels,
    test_decile_returns_monotonicity,
    test_ridge_recovers_signal_and_handles_constant_column,
    test_ridge_fills_nan_with_train_mean_only,
    test_ridge_predict_before_fit_raises,
    test_model_state_round_trip_is_bitwise_identical,
    test_load_model_refuses_unknown_and_missing_backend,
    test_make_model_does_not_silently_degrade,
    test_artifact_save_load_round_trip,
    test_artifact_rejects_empty_features_and_sanitizes_name,
    test_artifact_rejects_future_schema_version,
    test_availability_three_states,
    test_train_on_frame_finds_real_signal,
    test_train_on_frame_reports_no_edge_when_none_exists,
    test_train_on_frame_degrades_honestly,
    test_predict_frame_reports_missing_features,
)


def _run_all():
    """逐个执行并统计通过/失败,失败不中断后续用例。"""
    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    for fn in _TESTS:
        print(f"[{fn.__name__}]")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 需汇总所有失败原因
            failed.append((fn.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"  FAILED: {type(exc).__name__}: {exc}")
        else:
            passed.append(fn.__name__)

    print(f"\n通过 {len(passed)} / 失败 {len(failed)}(共 {len(_TESTS)})")
    if failed:
        for name, reason in failed:
            print(f"  - {name}: {reason}")
        raise SystemExit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    _run_all()
