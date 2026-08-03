"""多 agent 短线研判引擎。

流程(两级混合,参考 TradingAgents-CN 的分析师/辩论/风控协作思路,
但用原生 Python 编排,不引入任何图框架):

    1. 粗筛   —— 方法论 prompt 单次调用,把 N 只候选压缩成 depth 只
    2. 深度学习 —— 每只股票并行跑三位分析师(方法论 / 舆情 / 走势),
                   程序化加权汇总,选出 final_count 只
    3. 辩论   —— 每只最终股:多空辩论一回合 + 中性风控定稿

硬约束:
- 输入只用已入库数据(由调用方装配),引擎不碰数据库;
- 模型只能依据输入判断,无数据的小节由分析师如实标注,绝不编造;
- 任何一次模型输出解析失败都显式抛错,不做启发式兜底;
- 参数全部来自 AgentConfig,上限由配置钳制,防止超长任务打爆模型。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from engine.ai import (
    AIConfig,
    AIUnavailableError,
    OpenAICompatibleClient,
    describe,
    load_ai_config,
)
from engine.config import load_settings

# ------------------------------------------------------------------ 配置

class AgentConfigError(ValueError):
    """agent 段配置本身有问题。"""


class AgentOutputError(RuntimeError):
    """模型返回内容无法解析或结构不合法。调用方应如实上报失败。"""


@dataclass(frozen=True)
class AgentConfig:
    """多 agent 研判配置。字段与 settings.yaml 的 agent 段一一对应。

    enabled 独立于 ai.enabled:舆情研判和盘后复盘是两回事,开关分开。
    provider/model/base_url/api_key_env 允许留空回退到 ai 段。
    """

    enabled: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: str = "WORKBENCH_AI_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4000
    default_candidates: int = 200
    default_depth: int = 8
    default_final: int = 3
    max_candidates: int = 200
    max_depth: int = 30
    max_final: int = 10

    def clamp(self, candidates: int, depth: int, final_count: int) -> tuple[int, int, int]:
        """把面板参数钳制进合法区间。面板数字可以乱填,后端必须兜住。"""
        candidates = max(1, min(int(candidates), self.max_candidates))
        depth = max(1, min(int(depth), self.max_depth, candidates))
        final_count = max(1, min(int(final_count), self.max_final, depth))
        return candidates, depth, final_count

    def ai_config(self, ai: AIConfig) -> AIConfig:
        """合并出真正发给客户端的 AIConfig:agent 段优先,缺的字段回退 ai 段。"""
        return AIConfig(
            enabled=self.enabled,
            provider=self.provider or ai.provider,
            model=self.model or ai.model,
            base_url=self.base_url or ai.base_url,
            api_key_env=self.api_key_env or ai.api_key_env,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


def load_agent_config(settings: Optional[dict] = None) -> AgentConfig:
    """从 settings 的 agent 段构造配置。字段类型写错立刻报错,不静默忽略。"""
    applied: dict = settings if settings is not None else load_settings()
    raw: Any = applied.get("agent") or {}
    if not isinstance(raw, dict):
        raise AgentConfigError(f"agent 段应为映射,收到 {type(raw).__name__}")

    def _int(key: str, default: int) -> int:
        value = raw.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise AgentConfigError(f"agent.{key} 应为整数,收到 {value!r}") from exc

    return AgentConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider") or "").strip() or None,
        model=str(raw.get("model") or "").strip() or None,
        base_url=str(raw.get("base_url") or "").strip() or None,
        api_key_env=str(raw.get("api_key_env") or "").strip()
        or "WORKBENCH_AI_API_KEY",
        temperature=float(raw.get("temperature", 0.2)),
        max_tokens=_int("max_tokens", 4000),
        default_candidates=_int("default_candidates", 200),
        default_depth=_int("default_depth", 8),
        default_final=_int("default_final", 3),
        max_candidates=_int("max_candidates", 200),
        max_depth=_int("max_depth", 30),
        max_final=_int("max_final", 10),
    )


def status(config: AgentConfig, ai: AIConfig) -> dict:
    """研判功能可用性自述。与 ai.describe 三态口径一致。"""
    merged = config.ai_config(ai)
    info = describe(merged)
    info["agent_enabled"] = config.enabled
    info["defaults"] = {
        "candidates": config.default_candidates,
        "depth": config.default_depth,
        "final": config.default_final,
    }
    info["limits"] = {
        "max_candidates": config.max_candidates,
        "max_depth": config.max_depth,
        "max_final": config.max_final,
    }
    return info


# ------------------------------------------------------------------ 提示词

METHODOLOGY = """短线研判方法论(优先级从高到低):
1. 情绪周期:判断个股与所属板块处于 冰点修复/启动/发酵/加速/高潮/分歧/退潮 哪个阶段,二者是否共振;
   情绪启动+结构初期最有短线弹性,高潮末端、退潮反抽一律降级。
2. 波浪结构:周线定大势、日线定节奏;浪型不清就说"不适合强行数浪",不要教条。
3. MACD 动能:金叉/死叉、零轴上下、红绿柱扩张收缩、是否背离。
4. 量价验证:突破/回调是否放量有效;缩量上涨、放量滞涨都要指出。
5. 资金确认:资金流只做确认与降级,不做总指挥。
短线口径:看未来 1~5 个交易日的弹性与爆发力,淡化长线基本面。
纪律:只能依据输入数据判断,输入没有的信息一律视为未知,不得编造。"""

_SYSTEM_COARSE = f"""你是 A 股短线选股总分析师。任务:从候选池中按短线潜力选出最值得深挖的 K 只。
{METHODOLOGY}
输入:每行一只股票,格式:
序号. 代码 名称 行业 | 收盘 涨跌幅% | 5日涨幅% 20日涨幅% | 量比 | MACD状态 | 资金确认
选股原则:优先情绪启动+结构初期+量价配合;排除高潮末端、退潮反抽、明显破位;
只依据输入排序,理由必须引用输入里的数据。
输出 JSON(只输出一个 JSON 对象,不要任何额外文字):
{{"selected": [{{"ts_code": "代码", "reason": "一句话理由"}}], "note": "一句话总体观察"}}"""

_SYSTEM_DEEP_METHODOLOGY = f"""你是方法论分析师,专长:情绪周期+波浪+MACD+量价。
{METHODOLOGY}
输入:一只股票的完整快照(日线指标/周线概要/资金流/所属行业舆情热度)。
输出 JSON(只输出 JSON 对象):
{{"score": 0到100的整数, "stance": "bullish|neutral|bearish", "points": ["判断要点,3~5条"], "risks": ["风险点,1~3条"]}}"""

_SYSTEM_DEEP_SENTIMENT = f"""你是舆情分析师,只做一件事:从输入舆情里判断短线情绪面。
{METHODOLOGY}
数据说明:输入舆情是双源互补(① TrendRadar 全网热榜已入库数据;② 质量评估字段:关联度/来源可信度/情绪/时效,参考 TradingAgents-CN 口径)。
过滤规则(借鉴 TradingAgents 新闻处理流水线):
1. 相关性:优先个股直接相关(relevance/关联置信度高),行业舆情只作为板块热度参考;
2. 时效性:优先近 30 天、发布/首次抓取时间清楚;
3. 可信度:优先来源可信度(base_credibility / credibility)高的条目,<0.3 已由系统剔除;
4. 紧急程度:重/特大事件优先提示。
输入里没有相关舆情时,如实写"无有效舆情",score 取 45~55 的保守区间,不得编造任何新闻。
输出 JSON(只输出 JSON 对象):
{{"score": 0到100的整数, "stance": "bullish|neutral|bearish", "points": ["舆情要点,3~5条"], "risks": ["风险点,1~3条"]}}"""

_SYSTEM_DEEP_TREND = f"""你是走势分析师,专长:短线趋势的量价结构与资金态度。
{METHODOLOGY}
输入:一只股票的日线/周线指标与最近资金流。
重点:量价是否健康、支撑压力在哪、资金是承接还是撤退、趋势能否延续。
输出 JSON(只输出 JSON 对象):
{{"score": 0到100的整数, "stance": "bullish|neutral|bearish", "points": ["走势要点,3~5条"], "risks": ["风险点,1~3条"]}}"""

_SYSTEM_DEBATE = f"""你是多空辩论研究员。基于三位分析师(方法论/舆情/走势)对同一只股票的观点,组织一场辩论:
先写多方最强理由(2~3条),再写空方最强理由(2~3条)。只使用输入中已有的信息,不得新增事实。
输出 JSON(只输出 JSON 对象):
{{"bull": "多方理由,分条用分号隔开", "bear": "空方理由,分条用分号隔开"}}"""

_SYSTEM_RISK = f"""你是最终决策人,立场中性,只做风控定稿。综合多空辩论与三位分析师观点,给出这只股票的最终短线结论。
原则:风险优先,宁可错过不可做错;回撤控制优先于收益弹性;观点矛盾时取保守一侧。
输出 JSON(只输出 JSON 对象):
{{"verdict": "看多|中性|看空", "score": 0到100的整数, "thesis": "不超过80字的核心逻辑", "risks": ["风险点,1~3条"], "action": "短线操作建议,不超过40字"}}"""


# ------------------------------------------------------------------ JSON 容错解析

def parse_json_response(text: str) -> dict:
    """从模型输出中提取 JSON 对象。

    容忍:代码围栏、首尾空白、JSON 尾逗号。不允许:内容缺失、没有完整对象、
    解析失败后猜补字段。解析不了就是解析不了,如实上抛。
    """
    if not text or not text.strip():
        raise AgentOutputError("模型返回了空内容")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise AgentOutputError(f"模型输出中没有完整 JSON 对象: {text[:200]!r}")
    body = cleaned[start : end + 1]
    body = re.sub(r",\s*([}\]])", r"\1", body)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(f"模型输出 JSON 解析失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentOutputError(f"模型输出应为 JSON 对象,收到 {type(parsed).__name__}")
    return parsed


def _num(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if num == num else default  # NaN 按缺失处理


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


# ------------------------------------------------------------------ 各阶段

ProgressFn = Callable[[str, int, int, str], None]


def _progress(on: Optional[ProgressFn], stage: str, step: int, total: int, message: str) -> None:
    if on:
        on(stage, step, total, message)


def _analyze_one(
    client: OpenAICompatibleClient, config: AgentConfig, snapshot: dict
) -> dict:
    """一只股票的深度学习:三位分析师并行,程序化加权汇总。"""
    stock = snapshot["stock"]
    payload = json.dumps(snapshot, ensure_ascii=False, default=str)
    specs = [
        ("methodology", _SYSTEM_DEEP_METHODOLOGY),
        ("sentiment", _SYSTEM_DEEP_SENTIMENT),
        ("trend", _SYSTEM_DEEP_TREND),
    ]

    def _call(spec):
        name, system = spec
        raw = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"股票快照:\n{payload}"},
            ],
            json_mode=True,
        )
        data = parse_json_response(raw)
        return name, data

    results: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(specs)), thread_name_prefix="agent-deep") as pool:
        futures = {pool.submit(_call, spec): spec[0] for spec in specs}
        for future in as_completed(futures):
            try:
                name, data = future.result()
            except AIUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单分析师失败即整批失败
                raise AgentOutputError(f"分析师 {futures[future]} 调用失败: {exc}") from exc
            results[name] = data

    def _parse(name: str) -> tuple[float, str, list, list]:
        data = results.get(name) or {}
        score = min(100.0, max(0.0, _num(data.get("score"))))
        stance = _text(data.get("stance"), "neutral")
        if stance not in ("bullish", "neutral", "bearish"):
            stance = "neutral"
        points = data.get("points") or []
        risks = data.get("risks") or []
        if not isinstance(points, list):
            points = []
        if not isinstance(risks, list):
            risks = []
        return score, stance, points, risks

    m_score, m_stance, m_points, m_risks = _parse("methodology")
    s_score, s_stance, s_points, s_risks = _parse("sentiment")
    t_score, t_stance, t_points, t_risks = _parse("trend")

    total = round(0.4 * m_score + 0.3 * s_score + 0.3 * t_score, 1)
    stance = "bullish" if total >= 60 else ("bearish" if total <= 40 else "neutral")
    return {
        "ts_code": stock["ts_code"],
        "name": stock.get("name", ""),
        "industry": stock.get("industry", ""),
        "score": total,
        "stance": stance,
        "scores": {"methodology": m_score, "sentiment": s_score, "trend": t_score},
        "points": m_points + s_points + t_points,
        "risks": m_risks + s_risks + t_risks,
        "analysts": {
            "methodology": {"score": m_score, "stance": m_stance, "points": m_points, "risks": m_risks},
            "sentiment": {"score": s_score, "stance": s_stance, "points": s_points, "risks": s_risks},
            "trend": {"score": t_score, "stance": t_stance, "points": t_points, "risks": t_risks},
        },
    }


def _debate_one(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    snapshot: dict,
    deep: dict,
) -> dict:
    """一只最终股:多空辩论一回合 + 中性风控定稿。"""
    debate_payload = {
        "stock": snapshot["stock"],
        "analysts": deep.get("analysts", {}),
        "deep_score": deep.get("score"),
        "deep_points": deep.get("points", []),
        "deep_risks": deep.get("risks", []),
    }
    payload = json.dumps(debate_payload, ensure_ascii=False, default=str)

    raw_debate = client.chat(
        [
            {"role": "system", "content": _SYSTEM_DEBATE},
            {"role": "user", "content": f"三位分析师观点:\n{payload}"},
        ],
        json_mode=True,
    )
    debate = parse_json_response(raw_debate)

    raw_final = client.chat(
        [
            {"role": "system", "content": _SYSTEM_RISK},
            {"role": "user", "content": f"多空辩论:\n{json.dumps(debate, ensure_ascii=False, default=str)}\n\n三位分析师观点:\n{payload}"},
        ],
        json_mode=True,
    )
    final = parse_json_response(raw_final)

    score = min(100.0, max(0.0, _num(final.get("score"))))
    verdict = _text(final.get("verdict"), "中性")
    if verdict not in ("看多", "中性", "看空"):
        verdict = "中性"
    thesis = _text(final.get("thesis"), "暂无核心逻辑")
    action = _text(final.get("action"))
    risks = final.get("risks") or []
    if not isinstance(risks, list):
        risks = []
    return {
        "ts_code": deep["ts_code"],
        "name": deep["name"],
        "industry": deep["industry"],
        "score": score,
        "stance": "bullish" if verdict == "看多" else ("bearish" if verdict == "看空" else "neutral"),
        "verdict": verdict,
        "thesis": thesis,
        "action": action,
        "risks": risks,
        "debate": {"bull": _text(debate.get("bull")), "bear": _text(debate.get("bear"))},
        "deep": deep,
    }


def coarse_screen(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    candidates: list[dict],
    depth: int,
    on_progress: Optional[ProgressFn] = None,
) -> list[dict]:
    """阶段一:单次调用,把候选池压缩成 depth 只。"""
    _progress(on_progress, "coarse", 0, 1, f"粗筛:共 {len(candidates)} 只候选")
    rows = []
    for index, item in enumerate(candidates, start=1):
        rows.append(
            "{index}. {ts_code} {name} {industry} | 收盘 {close} 涨跌 {pct}% | "
            "5日 {r5}% 20日 {r20}% | 量比 {vr} | MACD {macd} | 资金 {mf}".format(
                index=index,
                ts_code=item.get("ts_code", ""),
                name=item.get("name", ""),
                industry=item.get("industry", ""),
                close=item.get("close", ""),
                pct=item.get("pct_chg", ""),
                r5=item.get("pct_5d", ""),
                r20=item.get("pct_20d", ""),
                vr=item.get("volume_ratio", ""),
                macd=item.get("macd_state", ""),
                mf=item.get("money_class", ""),
            )
        )
    raw = client.chat(
        [
            {"role": "system", "content": _SYSTEM_COARSE},
            {"role": "user", "content": f"候选池({len(rows)} 只),选出 {depth} 只:\n" + "\n".join(rows)},
        ],
        json_mode=True,
    )
    data = parse_json_response(raw)
    selected = data.get("selected") or []
    if not isinstance(selected, list):
        raise AgentOutputError("粗筛输出 selected 不是列表")
    by_code = {str(item.get("ts_code", "")).strip(): item for item in candidates}
    out: list[dict] = []
    seen: set[str] = set()
    for entry in selected:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("ts_code") or "").strip()
        if not code or code in seen or code not in by_code:
            continue  # 模型给了不存在的代码,忽略(不硬错,结果只含真实候选)
        seen.add(code)
        out.append(
            {
                "ts_code": code,
                "name": by_code[code].get("name", ""),
                "industry": by_code[code].get("industry", ""),
                "reason": _text(entry.get("reason")),
            }
        )
        if len(out) >= depth:
            break
    _progress(on_progress, "coarse", 1, 1, f"粗筛完成,进入深度学习 {len(out)} 只")
    return out


def deep_analyze(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    snapshots: dict[str, dict],
    on_progress: Optional[ProgressFn] = None,
) -> list[dict]:
    """阶段二:并行深度学习全部幸存股,返回按综合分降序的结果。"""
    codes = list(snapshots.keys())
    total = len(codes)
    results: list[dict] = []
    lock = threading.Lock()
    done = 0

    def _work(code: str) -> dict:
        nonlocal done
        snapshot = snapshots[code]
        stock = snapshot["stock"]
        result = _analyze_one(client, config, snapshot)
        with lock:
            done += 1
            _progress(on_progress, "deep", done, total, f"深度学习 {stock.get('name', code)}")
        return result

    with ThreadPoolExecutor(max_workers=min(4, max(1, total)), thread_name_prefix="agent-deep") as pool:
        futures = {pool.submit(_work, code): code for code in codes}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def debate_final(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    snapshots: dict[str, dict],
    deep_results: list[dict],
    final_count: int,
    on_progress: Optional[ProgressFn] = None,
) -> list[dict]:
    """阶段三:对综合分最高的 final_count 只并行辩论+风控,按最终分降序。"""
    finalists = deep_results[:final_count]
    total = len(finalists)
    out: list[dict] = []
    lock = threading.Lock()
    done = 0

    def _work(deep: dict) -> dict:
        nonlocal done
        code = deep["ts_code"]
        result = _debate_one(client, config, snapshots[code], deep)
        with lock:
            done += 1
            _progress(on_progress, "debate", done, total, f"辩论 {deep.get('name', code)}")
        return result

    with ThreadPoolExecutor(max_workers=min(3, max(1, total)), thread_name_prefix="agent-debate") as pool:
        futures = {pool.submit(_work, deep): deep["ts_code"] for deep in finalists}
        for future in as_completed(futures):
            out.append(future.result())

    out.sort(key=lambda item: item["score"], reverse=True)
    for rank, item in enumerate(out, start=1):
        item["rank"] = rank
    return out


def run_single(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    *,
    as_of: str,
    snapshot: dict,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    """单只股票深度研判:三位分析师 + 多空辩论 + 风控,不经过粗筛。

    与 run_judge 的区别:输入只有一只股票的完整快照,跳过候选池与粗筛,
    直接给出该股的短线结论。
    """
    if not snapshot or not snapshot.get("stock"):
        raise AgentOutputError("单只研判快照为空,没有可研判的股票")

    stock = snapshot["stock"]
    code = stock.get("ts_code", "")
    name = stock.get("name", code)
    _progress(on_progress, "deep", 1, 2, f"深度学习 {name}")
    deep = _analyze_one(client, config, snapshot)
    _progress(on_progress, "deep", 2, 2, f"深度学习完成 {name}")

    _progress(on_progress, "debate", 1, 1, f"多空辩论 {name}")
    final = _debate_one(client, config, snapshot, deep)
    final["rank"] = 1

    _progress(on_progress, "done", 1, 1, "研判完成")
    return {
        "as_of": as_of,
        "mode": "single",
        "candidates_limit": 1,
        "depth": 1,
        "final_count": 1,
        "coarse": [
            {
                "ts_code": code,
                "name": name,
                "industry": stock.get("industry", ""),
                "reason": "个股研判:直接进入深度分析",
            }
        ],
        "deep": [deep],
        "final": [final],
    }


def run_judge(
    client: OpenAICompatibleClient,
    config: AgentConfig,
    *,
    as_of: str,
    candidates: list[dict],
    loader: Callable[[str], dict],
    candidates_limit: int,
    depth: int,
    final_count: int,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    """完整研判:粗筛 → 深度学习 → 辩论,返回批次结果。

    loader(ts_code) -> snapshot:由调用方按需装配完整快照(深度学习与辩论
    阶段才会加载,粗筛只消耗候选池的紧凑行)。
    """
    candidates_limit, depth, final_count = config.clamp(
        candidates_limit, depth, final_count
    )
    pool = candidates[:candidates_limit]
    if not pool:
        raise AgentOutputError("候选池为空,没有可研判的股票")

    selected = coarse_screen(client, config, pool, depth, on_progress)
    if not selected:
        raise AgentOutputError("粗筛没有选出任何股票")

    snapshots: dict[str, dict] = {}
    for item in selected:
        snapshots[item["ts_code"]] = loader(item["ts_code"])

    deep_results = deep_analyze(client, config, snapshots, on_progress)
    final = debate_final(client, config, snapshots, deep_results, final_count, on_progress)

    _progress(on_progress, "done", 1, 1, "研判完成")
    return {
        "as_of": as_of,
        "candidates_limit": candidates_limit,
        "depth": depth,
        "final_count": final_count,
        "coarse": selected,
        "deep": deep_results,
        "final": final,
    }


__all__ = [
    "AgentConfig",
    "AgentConfigError",
    "AgentOutputError",
    "load_agent_config",
    "status",
    "run_judge",
    "run_single",
    "coarse_screen",
    "deep_analyze",
    "debate_final",
    "parse_json_response",
    "METHODOLOGY",
]
