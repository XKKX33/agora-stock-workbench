"""机器学习复核层。

定位:**复核**,不是**替代**。规则打分(score.py)仍是决策主体,
本层只回答一个问题——"历史上,这套因子对未来 N 日收益到底有没有区分度"。

四条纪律(与全项目一致,不得放宽):

1. 无前视。标签用 trade_cal 的市场交易日定位 T+N,不用个股自己的可用K线
   (否则停牌股会把"下一根K线"顶替成很久以后,污染 IC 与胜率)。
2. 训练/测试之间必须 purge。样本 t 的标签跨越 t..t+N,若测试集紧贴训练集
   末尾,这 N 天的标签就已包含测试期信息——必须切掉。见 splits.py。
3. 算不出就报 None,不报 0。样本不足的 IC、没有负样本的 AUC 都是"算不出",
   与"等于零"是两件事,前端必须能分开显示。见 metrics.py。
4. 没有训练好的模型时,报 available=False + missing_reason,
   绝不展示编造的预测概率。见 registry.py。

依赖策略:lightgbm/sklearn 是**可选**依赖。缺失时自动降级为本模块自带的
纯 numpy 岭回归,功能不缺失、只是模型更简单,并在产物元数据里如实记录
backend 名称,避免把"降级模型"讲成"GBDT 模型"。
"""

from __future__ import annotations

__all__ = ["labels", "splits", "metrics", "dataset", "model", "train", "registry"]
