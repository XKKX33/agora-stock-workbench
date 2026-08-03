from __future__ import annotations

import json

import pandas as pd

from app.repositories.market import MarketRepository
from app.services.news import NewsService
from app.services.stocks import StocksService
from engine.ml import registry as ml_registry

# 机器学习复核默认读的产物名。训练脚本 --name 与此一致才会被页面认到。
ML_ARTIFACT_NAME = "factor_ml"


class AnalyticsService:
    def __init__(self, repository: MarketRepository) -> None:
        self.repository = repository
        self.stocks = StocksService(repository)
        self.news = NewsService(repository)

    def _machine_learning(self) -> dict:
        """机器学习复核状态。三态由产物自带的样本外指标判定,不写死。

        判定逻辑全部在 engine.ml.registry.evaluate_availability 里:
        没产物 -> not_trained;有产物但样本外 IC / 截面数不达标 -> pending
        且**不返回任何预测值**。这里只做一件事:把它如实转出去。

        产物读不出来(文件损坏 / backend 不认识)时按 not_trained 上报并
        带上原因,而不是让分析接口整个 500 —— 因子覆盖那部分数据是好的。
        """
        try:
            artifact = ml_registry.load_artifact(ML_ARTIFACT_NAME)
        except Exception as exc:
            return {
                "availability": "not_trained",
                "reason": f"模型产物无法加载: {exc}",
                "backend": None,
                "metrics": {},
                "thresholds": ml_registry.thresholds(),
            }
        state = ml_registry.evaluate_availability(artifact)
        state["thresholds"] = ml_registry.thresholds()
        state["artifact_name"] = ML_ARTIFACT_NAME
        if artifact is not None:
            state["diagnostics"] = self._ml_diagnostics(artifact)
        return state

    @staticmethod
    def _ml_diagnostics(artifact: "ml_registry.Artifact") -> dict:
        """产物自带的诊断信息:折、逐日 IC、分桶、数据集口径、特征顺序。

        这些是"能不能信这个模型"的依据,不是预测值——所以即便
        availability 是 pending 也照样给,让页面能显示"差在哪"。
        权重(state 段)不外传:前端用不到,传了只是泄漏面。
        """
        payload = artifact.payload
        metrics = artifact.metrics
        return {
            "horizon": artifact.horizon,
            "trained_at": artifact.trained_at,
            "features": artifact.features,
            "dataset": artifact.dataset,
            "folds": list(payload.get("folds") or []),
            "params": dict(payload.get("params") or {}),
            "daily_ic": list(metrics.get("daily_ic") or []),
            "buckets": list(metrics.get("buckets") or []),
            "train_ic": metrics.get("train_ic"),
            "overfit_gap": metrics.get("overfit_gap"),
            "monotonic": metrics.get("monotonic"),
        }

    def sentiment(self) -> dict:
        run, rows = self.repository.latest_scan_rows()
        money_counts = rows["money_class"].fillna("未确认").value_counts().to_dict()
        return {
            "as_of": run["as_of"],
            "market_stage": self._market_stage(rows),
            "industries": run.get("top_industries", []),
            "money_classes": money_counts,
            "news_sentiment": self._news_sentiment(str(run["as_of"])),
            "industry_moneyflow": self._industry_moneyflow(),
        }

    def _industry_moneyflow(self) -> dict:
        """行业资金流向:取资金流最新的交易日的行业聚合,附覆盖区间。

        数据只有 11 天时如实给出 date_range 和当天覆盖股票数,页面据此
        标注口径,不假装是长期统计。行业缺失归为 industry=None(未知)。
        """
        start, end = self.repository.moneyflow_date_range()
        if not end:
            return {
                "availability": "unavailable",
                "reason": "资金流数据尚未采集",
                "as_of": None,
                "date_range": None,
                "stock_count": 0,
                "items": [],
            }
        frame = self.repository.moneyflow_industry_summary(end, limit=200)
        if frame.empty:
            return {
                "availability": "unavailable",
                "reason": "最新资金流交易日没有记录",
                "as_of": end,
                "date_range": [start, end],
                "stock_count": 0,
                "items": [],
            }
        items = []
        for _, row in frame.iterrows():
            industry = row["industry"]
            items.append({
                "industry": None if pd.isna(industry) else industry,
                "stock_count": int(row["stock_count"]),
                "net_mf_amount": float(row["net_mf_amount"]),
                "buy_lg_amount": float(row["buy_lg_amount"]),
                "sell_lg_amount": float(row["sell_lg_amount"]),
                "buy_elg_amount": float(row["buy_elg_amount"]),
                "sell_elg_amount": float(row["sell_elg_amount"]),
            })
        return {
            "availability": "available",
            "reason": None,
            "as_of": end,
            "date_range": [start, end],
            "stock_count": int(frame["stock_count"].sum()),
            "items": items,
        }

    def _news_sentiment(self, as_of: str) -> dict:
        """舆情情绪分布。舆情链路已接入,不再是一句写死的"尚未接入"。

        没有数据时按 digest 给出的 missing_reason 原样上报:是"来源没登记"、
        "从没采过",还是"当天没条目",这三件事页面要能分开显示。
        """
        digest = self.news.digest(as_of)
        if not digest["available"]:
            return {
                "availability": "unavailable",
                "trade_date": digest["trade_date"],
                "missing_reason": digest["missing_reason"],
                "detail": digest["detail"],
                "coverage": digest["coverage"],
                "counts": None,
            }
        # 两种"中性"必须分开:sentiment="neutral" 是有依据判出的中性,
        # sentiment=None 是判不出来。合并成一个数字会把"没结论"讲成"没倾向"。
        counts = {"positive": 0, "negative": 0, "neutral": 0, "undecided": 0}
        for item in digest["items"]:
            label = item["judgement"]["sentiment"] or "undecided"
            counts[label] = counts.get(label, 0) + 1
        return {
            "availability": "available",
            "trade_date": digest["trade_date"],
            "missing_reason": None,
            "detail": None,
            "coverage": digest["coverage"],
            "counts": counts,
            "sample_count": len(digest["items"]),
            # 情绪是规则推出的待验证判断,不是原文事实,页面别当结论用
            "label": "unverified",
        }

    def factors(self) -> dict:
        run, rows = self.repository.latest_scan_rows()
        factors: dict[str, list[float]] = {}
        for value in rows["contrib_json"].fillna("{}"):
            for name, score in json.loads(value).items():
                factors.setdefault(name, []).append(float(score))
        return {
            "as_of": run["as_of"],
            "factors": [
                {
                    "name": name,
                    "coverage": len(values) / max(len(rows), 1),
                    "average_contribution": sum(values) / len(values),
                }
                for name, values in sorted(factors.items())
            ],
            "machine_learning": self._machine_learning(),
        }

    def factor_detail(self, ts_code: str) -> dict:
        detail = self.stocks.detail(ts_code)
        return {
            "ts_code": detail["ts_code"],
            "name": detail["name"],
            "as_of": detail["as_of"],
            "factors": detail["factors"],
            "features": detail["features"],
            "category_scores": detail["category_scores"],
            "machine_learning": self._machine_learning(),
        }

    def ledger(self, strategy: str | None, page: int, per_page: int) -> dict:
        frame = self.repository.picks(strategy)
        total = len(frame)
        start = (page - 1) * per_page
        page_frame = frame.iloc[start : start + per_page]
        return {
            "items": self._records(page_frame),
            "meta": {"page": page, "per_page": per_page, "total": total},
        }

    def ledger_summary(self, strategy: str | None) -> dict:
        frame = self.repository.picks(strategy)
        return {
            "total": len(frame),
            "ret1": self._return_summary(frame, "ret1"),
            "ret3": self._return_summary(frame, "ret3"),
            "ret5": self._return_summary(frame, "ret5"),
            "ret10": self._return_summary(frame, "ret10"),
        }

    @staticmethod
    def _market_stage(rows: pd.DataFrame) -> dict:
        passed_ratio = float(rows["passed"].mean()) if len(rows) else 0.0
        if passed_ratio >= 0.35:
            label = "结构偏强"
        elif passed_ratio >= 0.15:
            label = "结构分化"
        else:
            label = "结构偏弱"
        return {"label": label, "passed_ratio": passed_ratio}

    @staticmethod
    def _return_summary(frame: pd.DataFrame, column: str) -> dict:
        if column not in frame:
            return {"sample_count": 0, "average": None, "positive_ratio": None}
        series = pd.to_numeric(frame[column], errors="coerce").dropna()
        if series.empty:
            return {"sample_count": 0, "average": None, "positive_ratio": None}
        return {
            "sample_count": int(len(series)),
            "average": float(series.mean()),
            "positive_ratio": float((series > 0).mean()),
        }

    @staticmethod
    def _records(frame: pd.DataFrame) -> list[dict]:
        clean = frame.where(pd.notna(frame), None)
        return clean.to_dict(orient="records")
