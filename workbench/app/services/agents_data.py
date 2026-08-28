"""`AgentJudgeManager` 的数据装配方法(mixin)。

从 `app/services/agents.py` 拆出来的,原因是那个文件到了 883 行,
超出项目自定的 800 行上限。拆分口径按**职责**:这里只做
"库里的行情/技术指标/资金流/舆情 → 喂给模型的紧凑快照",
不碰任务编排、不碰 agent_runs/agent_judgments 落库、不碰 AI 客户端。

为什么用 mixin 而不是组合:外部只 import `AgentJudgeManager`
(app/api/agents.py 与 app/main.py 两处),mixin 让它的方法集合
与拆分前逐个相等,调用侧一行不用动。

纪律(与 engine/db.py 一致):
- 所有读路径 `ensure_schema=False`,不建表、不写库。
- 算不出就留 None,绝不用 0 冒充"中性""无"——`_round` 对 NaN/None 原样返回。
- 低可信度舆情(credibility < 0.3)直接剔除,不喂给模型。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from engine.db import Store
from app.schemas.pi_agent import compute_candidate_hash, compute_input_hash


@dataclass(frozen=True)
class FrozenAgentInput:
    """一次研判使用的候选和完整快照。

    数据在构造时全部读取完毕；后续 Pi 调用只消费这里的内存副本，不重新查询
    DuckDB。列表保留 JSON 数组形状，哈希记录本次冻结输入的审计指纹。
    """

    as_of: str
    scan_run_id: str
    candidates: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    candidate_hash: str
    input_hash: str

    @property
    def snapshot_by_code(self) -> dict[str, dict[str, Any]]:
        return {str(item["ts_code"]): item for item in self.snapshots}


class AgentDataMixin:
    """行情/指标/资金流/舆情 → 模型输入快照。

    依赖宿主类提供 `self.repository`(MarketRepository)与 `self.db_path`。
    """

    def _build_pool(
        self, candidates_n: int, ts_codes: Optional[list[str]], as_of: str
    ) -> list[dict]:
        """构造粗筛输入:最近扫描候选(可按 ts_codes 收窄),每只带紧凑行情行。"""
        run, frame = self.repository.latest_scan_rows()
        data = frame
        if ts_codes:
            wanted = {c.strip().upper() for c in ts_codes if c and c.strip()}
            data = data[data["ts_code"].isin(wanted)]
        pool: list[dict] = []
        for _, row in data.head(candidates_n).iterrows():
            code = row["ts_code"]
            try:
                history = self.repository.history(code, as_of, 40)
            except Exception:
                history = pd.DataFrame()
            pool.append(self._compact_row(row, history))
        return pool

    def freeze_agent_input(
        self,
        candidates_n: int,
        ts_codes: Optional[list[str]],
        as_of: str,
        *,
        run_id: str | None = None,
        strategy: str | None = None,
    ) -> FrozenAgentInput:
        """冻结 Pi 输入;显式 run_id 时只读取并校验该批次。"""
        ensure_visible = getattr(self, "_ensure_visible_as_of", None)
        if callable(ensure_visible):
            as_of = ensure_visible(str(as_of))
        if isinstance(candidates_n, bool) or not isinstance(candidates_n, int):
            raise ValueError("candidates_n 必须是整数")
        if candidates_n < 1 or candidates_n > 20:
            raise ValueError("candidates_n 必须在 1~20 之间")
        if not str(as_of).strip():
            raise ValueError("as_of 不能为空")

        if run_id is not None:
            exact_reader = getattr(self.repository, "scan_batch", None)
            if exact_reader is None:
                exact_reader = getattr(self.repository, "scan_rows_exact", None)
            if exact_reader is None:
                raise RuntimeError("扫描仓储不支持精确批次读取")
            scan_run, frame = exact_reader(run_id, as_of=as_of, strategy=strategy)
        else:
            scan_run, frame = self.repository.latest_scan_rows()
            if str(scan_run.get("as_of")) != str(as_of):
                raise RuntimeError(
                    f"扫描批次 {scan_run.get('run_id')} 日期不匹配: 期望 {as_of}, 实际 {scan_run.get('as_of')}"
                )
            if strategy is not None and str(scan_run.get("strategy")) != str(strategy):
                raise RuntimeError(
                    f"扫描批次 {scan_run.get('run_id')} 策略不匹配: 期望 {strategy}, 实际 {scan_run.get('strategy')}"
                )
        if run_id is not None and str(scan_run.get("run_id") or "") != str(run_id):
            raise RuntimeError(
                f"扫描批次身份不匹配: 期望 {run_id}, 实际 {scan_run.get('run_id')}"
            )
        if str(scan_run.get("as_of")) != str(as_of):
            raise RuntimeError("扫描批次日期不匹配")
        if str(scan_run.get("status") or "").lower() == "failed":
            raise RuntimeError(f"扫描批次 {scan_run.get('run_id')} 已失败")
        if frame is None or frame.empty:
            raise RuntimeError("扫描候选池为空,无法冻结 Agent 输入")
        data = frame
        if ts_codes:
            wanted = {
                str(code).strip().upper()
                for code in ts_codes
                if str(code).strip()
            }
            available = {
                str(code).strip().upper()
                for code in data.get("ts_code", pd.Series(dtype=str)).tolist()
            }
            missing = sorted(wanted - available)
            if missing:
                raise RuntimeError(f"请求股票不在扫描候选池: {','.join(missing)}")
            data = data[data["ts_code"].astype(str).str.upper().isin(wanted)]
        data = data.head(candidates_n)
        if data.empty:
            raise RuntimeError("没有可冻结的扫描候选")

        candidates: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in data.iterrows():
            code = str(row.get("ts_code") or "").strip().upper()
            if not code:
                raise RuntimeError("扫描候选缺少 ts_code")
            if code in seen:
                raise RuntimeError(f"扫描候选存在重复股票: {code}")
            seen.add(code)

            # 粗筛数据也必须来自冻结时点；异常和空结果都不能静默跳过。
            history = self.repository.history(code, as_of, 150)
            if history is None or history.empty:
                raise RuntimeError(f"冻结候选 {code} 缺少 Agent 粗筛所需历史行情")
            compact = self._compact_row(row, history.tail(40))
            total = _json_number(row.get("total"))
            if total is None:
                raise RuntimeError(f"冻结候选 {code} 缺少有效规则评分")
            candidate = {
                **compact,
                "rank": int(row["rank"]) if "rank" in row and pd.notna(row["rank"]) else len(candidates) + 1,
                "total": total,
                "score": total,
            }
            candidates.append(candidate)

            try:
                snapshot = self._load_snapshot(code, as_of)
            except Exception:
                raise
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("stock"), dict):
                raise RuntimeError(f"完整快照 {code} 无效")
            stock = snapshot["stock"]
            snapshot_code = str(stock.get("ts_code") or "").strip().upper()
            if snapshot_code != code:
                raise RuntimeError(f"完整快照 {code} 股票归属不一致")
            snapshot = {"ts_code": code, **snapshot}
            snapshots.append(snapshot)

        frozen_candidates = copy.deepcopy(candidates)
        frozen_snapshots = copy.deepcopy(snapshots)
        return FrozenAgentInput(
            as_of=str(as_of),
            scan_run_id=str(scan_run.get("run_id") or ""),
            candidates=frozen_candidates,
            snapshots=frozen_snapshots,
            candidate_hash=compute_candidate_hash(frozen_candidates),
            input_hash=compute_input_hash(frozen_candidates, frozen_snapshots),
        )

    @staticmethod
    def _compact_row(row: pd.Series, history: pd.DataFrame) -> dict:
        """粗筛用的紧凑行:收盘/涨跌/5日/20日/量比/MACD状态/资金确认。"""
        close = None
        pct = None
        pct_5d = None
        pct_20d = None
        macd_state = ""
        if not history.empty:
            closes = history["close"].astype(float)
            close = float(closes.iloc[-1])
            pct = float(history["pct_chg"].iloc[-1]) if pd.notna(history["pct_chg"].iloc[-1]) else None
            if len(closes) >= 6:
                pct_5d = round((closes.iloc[-1] / closes.iloc[-6] - 1) * 100, 2)
            if len(closes) >= 21:
                pct_20d = round((closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2)
            # 和上面 pct_5d/pct_20d 一样要看够不够样本:dif 要 26 根、dea 再要 9 个 dif,
            # 不足 34 根算出的"零轴上红柱"是预热噪音,喂给模型等于给它一个假事实。
            if len(closes) >= 34:
                macd_state = AgentDataMixin._macd_state(closes)
        return {
            "ts_code": row["ts_code"],
            "name": row["name"] if pd.notna(row["name"]) else "",
            "industry": row["industry"] if pd.notna(row["industry"]) else "",
            "close": close,
            "pct_chg": pct,
            "pct_5d": pct_5d,
            "pct_20d": pct_20d,
            "volume_ratio": None,
            "macd_state": macd_state,
            "money_class": row["money_class"] if pd.notna(row["money_class"]) else "",
        }

    @staticmethod
    def _macd_state(closes: pd.Series) -> str:
        """把日线 MACD 压成一句话状态,粗筛只喂状态不喂原始序列。"""
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        last_dif, last_dea = float(dif.iloc[-1]), float(dea.iloc[-1])
        prev_dif = float(dif.iloc[-2]) if len(dif) > 1 else last_dif
        cross = "金叉" if prev_dif <= last_dea and last_dif > last_dea else (
            "死叉" if prev_dif >= last_dea and last_dif < last_dea else ""
        )
        zone = "零轴上" if last_dif >= 0 else "零轴下"
        return f"{zone}{cross or ('红柱' if last_dif >= last_dea else '绿柱')}"

    def _load_snapshot(self, ts_code: str, as_of: str) -> dict:
        """深度学习/辩论用的完整快照:行情+指标+周线+资金流+舆情。"""
        history = self.repository.history(ts_code, as_of, 150)
        moneyflow = self.repository.moneyflow(ts_code, as_of, 10)
        with Store(self.db_path, ensure_schema=False) as store:
            row = store.con.execute(
                "SELECT ts_code, symbol, name, industry FROM stock_basic WHERE ts_code = ?",
                [ts_code],
            ).fetchone()
            info = (
                dict(zip(("ts_code", "symbol", "name", "industry"), row))
                if row
                else {"ts_code": ts_code, "name": "", "industry": ""}
            )
            stock_news = store.news_for_link(
                link_type="stock", link_key=ts_code, as_of=as_of, limit=15
            )
            industry = info.get("industry") or ""
            industry_news = (
                store.news_for_link(
                    link_type="industry", link_key=industry, as_of=as_of, limit=8
                )
                if industry
                else pd.DataFrame()
            )
        return {
            "stock": self._stock_brief(info, history),
            "daily": self._daily_brief(history),
            "weekly": self._weekly_brief(history),
            "moneyflow": self._moneyflow_brief(moneyflow),
            "news": {
                "source_note": "舆情输入双源互补:① TrendRadar 全网热榜已入库数据;② 质量评估字段(关联度/来源可信度/情绪/时效)借鉴 TradingAgents-CN 口径。未新增采集器。",
                "stock_items": self._news_brief(stock_news),
                "industry_items": self._news_brief(industry_news),
            },
        }

    @staticmethod
    def _stock_brief(info: dict, history: pd.DataFrame) -> dict:
        close = None
        if not history.empty:
            close = round(float(history["close"].iloc[-1]), 2)
        return {
            "ts_code": info.get("ts_code"),
            "name": info.get("name"),
            "industry": info.get("industry"),
            "close": close,
        }

    @staticmethod
    def _daily_brief(history: pd.DataFrame) -> dict:
        if history.empty:
            return {}
        closes = history["close"].astype(float)
        highs = history["high"].astype(float)
        lows = history["low"].astype(float)
        n_bars = len(closes)
        # 样本不够就不给数。tail(60).mean() 在只有 9 根时会算出 9 根的均值却仍叫 ma60,
        # 数值看着正常、含义是错的——这种字段喂给模型比缺字段危险得多。
        ma = {
            f"ma{n}": _round(float(closes.tail(n).mean()), 2) if n_bars >= n else None
            for n in (5, 10, 20, 60)
        }

        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        hist = (dif - dea) * 2

        low9 = lows.rolling(9).min()
        high9 = highs.rolling(9).max()
        rsv = (closes - low9) / (high9 - low9).replace(0, np.nan) * 100
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        j = 3 * k - 2 * d

        diff = closes.diff()
        up = diff.clip(lower=0)
        down = -diff.clip(upper=0)
        avg_up = up.ewm(alpha=1 / 6, adjust=False).mean()
        avg_down = down.ewm(alpha=1 / 6, adjust=False).mean()
        rsi6 = 100 - 100 / (1 + avg_up / avg_down.replace(0, np.nan))

        mid = closes.rolling(20).mean()
        std = closes.rolling(20).std()

        recent = history.tail(5)
        recent_5 = [
            {
                "date": str(r["trade_date"]),
                "pct_chg": _round(float(r["pct_chg"]), 2) if pd.notna(r["pct_chg"]) else None,
                "vol": _round(float(r["vol"]), 0) if pd.notna(r["vol"]) else None,
                "amount": _round(float(r["amount"]), 0) if pd.notna(r["amount"]) else None,
            }
            for _, r in recent.iterrows()
        ]
        return {
            "ma": ma,
            # 26 日 EMA 叠 9 日 DEA:35 根之前都是预热,不是指标
            "macd": {
                "dif": _round(float(dif.iloc[-1]), 3) if n_bars >= 26 else None,
                "dea": _round(float(dea.iloc[-1]), 3) if n_bars >= 34 else None,
                "hist": _round(float(hist.iloc[-1]), 3) if n_bars >= 34 else None,
            },
            "kdj": {
                "k": _round(float(k.iloc[-1]), 2) if n_bars >= 9 else None,
                "d": _round(float(d.iloc[-1]), 2) if n_bars >= 9 else None,
                "j": _round(float(j.iloc[-1]), 2) if n_bars >= 9 else None,
            },
            "rsi6": _round(float(rsi6.iloc[-1]), 2)
            if n_bars >= 6 and pd.notna(rsi6.iloc[-1])
            else None,
            "boll": {
                "upper": _round(float(mid.iloc[-1] + 2 * std.iloc[-1]), 2) if pd.notna(std.iloc[-1]) else None,
                "mid": _round(float(mid.iloc[-1]), 2),
                "lower": _round(float(mid.iloc[-1] - 2 * std.iloc[-1]), 2) if pd.notna(std.iloc[-1]) else None,
            },
            "recent_5": recent_5,
            "range_20": {
                "high": _round(float(highs.tail(20).max()), 2) if n_bars >= 20 else None,
                "low": _round(float(lows.tail(20).min()), 2) if n_bars >= 20 else None,
                "pct_20d": _round((closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2)
                if n_bars >= 21
                else None,
            },
        }

    @staticmethod
    def _weekly_brief(history: pd.DataFrame) -> dict:
        if history.empty or len(history) < 5:
            return {}
        df = history.copy()
        df["_dt"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["_dt"]).set_index("_dt")
        weekly = df["close"].astype(float).resample("W-FRI").last().dropna()
        if len(weekly) < 2:
            return {}
        weeks = weekly.tail(6)
        pcts = weeks.pct_change().iloc[1:] * 100
        last_6 = [
            {
                "week": str(w.date()),
                "close": _round(float(c), 2),
                "pct_chg": _round(float(p), 2) if pd.notna(p) else None,
            }
            for (w, c), (_, p) in zip(weeks.items(), pcts.items())
        ]
        # 周线 MACD 和日线同口径:dif 要 26 根周线、dea 再要 9 个 dif,即 34 周
        # (约 8 个月日线)。样本不够只给周涨跌,不给"周线多头/空头"结论。
        if len(weekly) < 34:
            return {"last_6": last_6, "macd_dif": None, "macd_dea": None, "trend": None}
        ema12 = weekly.ewm(span=12, adjust=False).mean()
        ema26 = weekly.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        trend = "周线多头" if dif.iloc[-1] > dea.iloc[-1] > 0 else (
            "周线空头" if dif.iloc[-1] < dea.iloc[-1] < 0 else "周线修复中"
        )
        return {
            "last_6": last_6,
            "macd_dif": _round(float(dif.iloc[-1]), 3),
            "macd_dea": _round(float(dea.iloc[-1]), 3),
            "trend": trend,
        }

    @staticmethod
    def _moneyflow_brief(frame: pd.DataFrame) -> dict:
        if frame.empty:
            return {"recent": [], "net_sum_5": None}
        rows = []
        for _, r in frame.tail(10).iterrows():
            net = r["net_mf_amount"]
            rows.append(
                {
                    "date": str(r["trade_date"]),
                    "net": _round(float(net), 0) if pd.notna(net) else None,
                    "lg": _round(float((r["buy_lg_amount"] or 0) - (r["sell_lg_amount"] or 0)), 0),
                    "elg": _round(float((r["buy_elg_amount"] or 0) - (r["sell_elg_amount"] or 0)), 0),
                }
            )
        net_sum = frame.tail(5)["net_mf_amount"]
        net_sum_5 = _round(float(net_sum.sum()), 0) if not net_sum.isna().all() else None
        return {"recent": rows, "net_sum_5": net_sum_5}

    @staticmethod
    def _news_brief(frame: pd.DataFrame) -> list[dict]:
        """舆情快照:借鉴 TradingAgents-CN 的质量评估口径,输出双源结构化条目。

        每个条目带来源(source_kind / source_name)、可信度、情绪、时效、
        关联置信度;低可信度(credibility < 0.3)记录直接剔除,不喂给模型。
        这属于"过滤 + 评估"而不是新增采集器。
        """
        items = []
        for _, r in frame.iterrows():
            credibility = r.get("credibility")
            if credibility is not None and pd.notna(credibility) and float(credibility) < 0.3:
                continue  # 低可信度条目不喂给模型
            
            title = r["title"] if pd.notna(r["title"]) else ""
            source_name = r.get("source_name") if pd.notna(r.get("source_name")) else ""
            source_kind = r.get("source_kind") if pd.notna(r.get("source_kind")) else "news"
            base_cr = r.get("base_credibility")
            base_cred = _round(float(base_cr), 2) if base_cr is not None and pd.notna(base_cr) else None
            link_conf = r.get("link_confidence")
            relevance = _round(float(link_conf), 2) if link_conf is not None and pd.notna(link_conf) else None
            sentiment = r["sentiment"] if pd.notna(r["sentiment"]) else None
            sentiment_score = r.get("sentiment_score")
            sentiment_val = _round(float(sentiment_score), 3) if sentiment_score is not None and pd.notna(sentiment_score) else None
            
            # 质量评分 = 关联度 + 来源可信度 + 情绪强度(仅供参考,不绝对)
            quality = None
            if relevance is not None or base_cred is not None:
                parts = [0.0]
                if relevance is not None:
                    parts.append(relevance * 0.6)
                if base_cred is not None:
                    parts.append(base_cred * 0.4)
                quality = _round(sum(parts), 2)

            items.append(
                {
                    "title": title,
                    "source": source_name,
                    "source_kind": source_kind,
                    "published": str(r["published_at"]) if pd.notna(r["published_at"]) else None,
                    "sentiment": sentiment,
                    "sentiment_score": sentiment_val,
                    "credibility": _round(float(credibility), 2) if credibility is not None and pd.notna(credibility) else None,
                    "base_credibility": base_cred,
                    "relevance": relevance,
                    "quality_score": quality,
                }
            )
        return items[:15]


def _round(value: float, digits: int = 2):
    """安全取整:NaN/None 原样返回,不做 0 冒充。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return round(num, digits)


def _json_number(value: Any) -> float | int | None:
    """将 pandas 数值转成严格 JSON 数字；缺失值保持 None，不填零。"""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number
