"""时序切分:purged walk-forward。

为什么不能用普通 KFold / train_test_split
------------------------------------------
样本 t 的标签跨越 t..t+N(N = 期限交易日数)。随机切分会把同一段未来
分到训练集和测试集两侧,模型于是"见过"测试期的价格,指标虚高但实盘无效。

本模块只提供一种切分:**按交易日切、训练在前、测试在后、中间挖掉 N 天**。

    |<---- train ---->| purge(N) |<-- test -->|
                       ^^^^^^^^^^
                       训练集末尾这 N 天的标签会伸进测试期,必须切掉

embargo 是 purge 之后的额外隔离带(默认 0)。当特征本身含滚动窗口
(本项目的 ret20/ret60/vol20_60 都是),测试期开头的特征会回看训练期尾部。
这个方向的泄漏不影响标签,但会让测试集与训练集高度相关、指标偏乐观,
需要保守评估时把 embargo 设为特征最长回看窗口。

切分对象是**交易日**而不是行:同一天的所有股票必须整天落在同一侧,
否则同日不同票的截面信息会跨越边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence

import pandas as pd


@dataclass(frozen=True)
class Fold:
    """一折切分的交易日边界(左闭右闭,便于直接与 as_of 字符串比较)。"""

    index: int
    train_days: tuple[str, ...]
    test_days: tuple[str, ...]
    purged_days: tuple[str, ...]

    @property
    def train_start(self) -> str:
        return self.train_days[0]

    @property
    def train_end(self) -> str:
        return self.train_days[-1]

    @property
    def test_start(self) -> str:
        return self.test_days[0]

    @property
    def test_end(self) -> str:
        return self.test_days[-1]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "n_train_days": len(self.train_days),
            "n_test_days": len(self.test_days),
            "n_purged_days": len(self.purged_days),
        }


def purged_walk_forward(
    days: Sequence[str],
    *,
    horizon_days: int,
    n_splits: int = 3,
    test_size: Optional[int] = None,
    min_train_days: int = 20,
    embargo_days: int = 0,
) -> List[Fold]:
    """生成 purged walk-forward 折。

    days        : 升序去重的交易日序列(样本实际覆盖的日子)
    horizon_days: 标签期限的交易日数,决定 purge 宽度
    test_size   : 每折测试天数;默认按剩余天数均分
    min_train_days: 训练集最少天数,不足则该折不产出(样本太少的模型没有意义)

    返回可能少于 n_splits 折——天数不够时**少给折**,不硬凑。
    """
    if horizon_days < 1:
        raise ValueError("horizon_days 必须 >= 1")
    if n_splits < 1:
        raise ValueError("n_splits 必须 >= 1")
    if embargo_days < 0:
        raise ValueError("embargo_days 不能为负")

    ordered = sorted({str(d) for d in days})
    total = len(ordered)
    # purge 宽度 = horizon_days:样本 t 的标签看到 t+horizon,
    # 故训练集末尾 horizon_days 天必须剔除。embargo 再往后推。
    gap = horizon_days + embargo_days
    if total == 0:
        return []

    if test_size is None:
        usable = total - min_train_days - gap
        if usable <= 0:
            return []
        test_size = max(1, usable // n_splits)

    folds: List[Fold] = []
    # 从后往前排布测试窗口,保证最后一折贴着最新数据(最贴近实盘状态)
    for k in range(n_splits):
        test_end_pos = total - k * test_size
        test_start_pos = test_end_pos - test_size
        if test_start_pos <= 0:
            break
        train_end_pos = test_start_pos - gap
        if train_end_pos < min_train_days:
            break

        train_days = tuple(ordered[:train_end_pos])
        purged = tuple(ordered[train_end_pos:test_start_pos])
        test_days = tuple(ordered[test_start_pos:test_end_pos])
        if not train_days or not test_days:
            break
        folds.append(Fold(
            index=len(folds),
            train_days=train_days,
            test_days=test_days,
            purged_days=purged,
        ))

    folds.reverse()  # 时间升序,便于阅读与展示
    return [
        Fold(index=i, train_days=f.train_days, test_days=f.test_days,
             purged_days=f.purged_days)
        for i, f in enumerate(folds)
    ]


def split_frame(frame: pd.DataFrame, fold: Fold, *, day_col: str = "as_of") -> tuple[pd.DataFrame, pd.DataFrame]:
    """按折边界切出 (训练集, 测试集)。整天归属,不拆同日样本。"""
    if day_col not in frame.columns:
        raise ValueError(f"缺少交易日列: {day_col}")
    days = frame[day_col].astype(str)
    train = frame[days.isin(set(fold.train_days))]
    test = frame[days.isin(set(fold.test_days))]
    return train, test


def assert_no_leakage(fold: Fold, horizon_days: int, calendar_days: Sequence[str]) -> None:
    """自检:训练集最后一天的标签窗口不得触及测试集第一天。

    这是 purge 正确性的守门断言。切分逻辑改动后若忘了同步 purge 宽度,
    这里会直接抛错,而不是安静地产出偏乐观的指标。
    """
    ordered = sorted({str(d) for d in calendar_days})
    index = {d: i for i, d in enumerate(ordered)}
    if fold.train_end not in index or fold.test_start not in index:
        raise ValueError("折边界不在给定日历内,无法校验泄漏")
    label_end_pos = index[fold.train_end] + horizon_days
    if label_end_pos >= index[fold.test_start]:
        raise AssertionError(
            f"purge 不足:训练末日 {fold.train_end} 的 T+{horizon_days} 标签"
            f"已进入测试期(测试首日 {fold.test_start})"
        )
