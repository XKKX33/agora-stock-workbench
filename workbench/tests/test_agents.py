"""多 agent 研判引擎单元测试。

覆盖目标:
- 粗筛/深度/辩论各阶段 prompt 组装与模型应答解析(JSON 容错);
- 阶段参数控制(clamp 钳制、上限约束);
- 0.4/0.3/0.3 加权汇总与最终排名;
- 模型输出不可解析/空内容时如实上抛,不做降级;
- 结果落库幂等(同 run_id+ts_code 重复写入不重复)。

运行:
    cd workbench
    python -m pytest tests/test_agents.py -q
"""

from __future__ import annotations

import json

import pytest

from engine.agents import (
    AgentConfig,
    AgentConfigError,
    AgentOutputError,
    coarse_screen,
    load_agent_config,
    parse_json_response,
    run_judge,
    run_single,
    status,
)
from engine.ai import AIConfig

pytestmark = pytest.mark.unit


class FakeClient:
    """按 system prompt 关键字返回 canned JSON 的假模型客户端。"""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[list[dict]] = []

    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature=None,
        max_tokens=None,
        retries: int = 2,
    ) -> str:
        self.calls.append(messages)
        system = messages[0]["content"] if messages else ""
        for key, payload in self.responses.items():
            if key in system:
                return payload
        raise AssertionError(f"没有为这个 system prompt 准备响应: {system[:80]}")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- JSON 容错


def test_parse_json_response_rejects_empty():
    with pytest.raises(AgentOutputError, match="空内容"):
        parse_json_response("")
    with pytest.raises(AgentOutputError, match="空内容"):
        parse_json_response("   \n  ")


def test_parse_json_response_accepts_code_fence_and_trailing_comma():
    raw = "```json\n{\"selected\": [{\"ts_code\": \"000001.SZ\"}],}\n```"
    data = parse_json_response(raw)
    assert data["selected"][0]["ts_code"] == "000001.SZ"


def test_parse_json_response_rejects_garbage_and_non_object():
    with pytest.raises(AgentOutputError, match="JSON"):
        parse_json_response("这是一段自然语言,不是 JSON")
    with pytest.raises(AgentOutputError, match="JSON 对象"):
        parse_json_response("[1, 2, 3]")


# ---------------------------------------------------------------- 配置与参数控制


def test_agent_config_clamp_respects_limits():
    config = AgentConfig(max_candidates=200, max_depth=30, max_final=10)
    assert config.clamp(500, 50, 20) == (200, 30, 10)


def test_agent_config_clamp_nested_constraints():
    config = AgentConfig()
    # depth 不能超过 candidates,final 不能超过 depth
    assert config.clamp(3, 5, 4) == (3, 3, 3)
    assert config.clamp(0, -1, -5) == (1, 1, 1)
    assert config.clamp("50", "10", "2") == (20, 10, 2)


def test_load_agent_config_defaults():
    config = load_agent_config({})
    assert config == AgentConfig()


def test_load_agent_config_custom_values():
    config = load_agent_config(
        {
            "agent": {
                "enabled": True,
                "provider": "openai_compatible",
                "model": "deepseek-chat",
                "base_url": "https://api.example.com/v1",
                "reasoning_effort": "low",
                "max_tokens": 1200,
                "default_candidates": 100,
                "default_depth": 10,
                "default_final": 5,
                "max_candidates": 300,
                "max_depth": 40,
                "max_final": 20,
            }
        }
    )
    assert config.enabled is True
    assert config.provider == "openai_compatible"
    assert config.model == "deepseek-chat"
    assert config.base_url == "https://api.example.com/v1"
    assert config.reasoning_effort == "low"
    assert config.max_tokens == 1200
    assert config.default_candidates == 100
    assert config.default_depth == 10
    assert config.default_final == 5
    assert config.max_candidates == 300
    assert config.max_depth == 40
    assert config.max_final == 20


def test_load_agent_config_rejects_bad_types():
    with pytest.raises(AgentConfigError):
        load_agent_config({"agent": "不是映射"})
    with pytest.raises(AgentConfigError):
        load_agent_config({"agent": {"max_candidates": "abc"}})


# ---------------------------------------------------------------- status 三态


def test_status_three_states(monkeypatch):
    env_key = "WORKBENCH_AGENTS_TEST_KEY"
    monkeypatch.delenv(env_key, raising=False)

    # 1. 明确关闭
    info = status(AgentConfig(enabled=False), AIConfig(enabled=False))
    assert info["availability"] == "disabled"
    assert info["agent_enabled"] is False

    # 2. 开着但缺凭据
    config = AgentConfig(
        enabled=True,
        provider="openai_compatible",
        model="m",
        base_url="http://127.0.0.1:8000/v1",
        api_key_env=env_key,
    )
    ai = config.ai_config(AIConfig())
    info = status(config, ai)
    assert info["availability"] == "unconfigured"
    assert "未设置" in info["reason"]

    # 3. 齐全
    monkeypatch.setenv(env_key, "sk-test")
    info = status(config, ai)
    assert info["availability"] == "available"
    assert info["defaults"] == {"candidates": 20, "depth": 20, "final": 3}
    assert info["limits"]["max_candidates"] == 20


# ---------------------------------------------------------------- 粗筛


def test_coarse_screen_ignores_unknown_codes():
    client = FakeClient(
        {
            "选股总分析师": json.dumps(
                {"selected": [{"ts_code": "999999.SZ", "reason": "不存在"}], "note": ""},
                ensure_ascii=False,
            )
        }
    )
    pool = [{"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行"}]
    out = coarse_screen(client, AgentConfig(), pool, depth=3)
    assert out == []


def test_coarse_screen_builds_prompt_rows():
    client = FakeClient(
        {
            "选股总分析师": json.dumps(
                {"selected": [{"ts_code": "000001.SZ", "reason": "情绪启动"}], "note": ""},
                ensure_ascii=False,
            )
        }
    )
    pool = [
        {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "industry": "银行",
            "close": 10.5,
            "pct_chg": 2.1,
            "pct_5d": 5.0,
            "pct_20d": 8.0,
            "volume_ratio": 1.5,
            "macd_state": "金叉",
            "money_class": "资金一致确认",
        }
    ]
    out = coarse_screen(client, AgentConfig(), pool, depth=1)
    assert out == [
        {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "reason": "情绪启动"}
    ]
    user_text = client.calls[0][1]["content"]
    assert "000001.SZ 平安银行 银行" in user_text
    assert "资金一致确认" in user_text
    assert "选出 1 只" in user_text


# ---------------------------------------------------------------- 完整流程


def _full_flow_responses() -> dict:
    return {
        "选股总分析师": json.dumps(
            {"selected": [{"ts_code": "000001.SZ", "reason": "情绪启动"}], "note": "ok"},
            ensure_ascii=False,
        ),
        "方法论分析师": json.dumps(
            {"score": 80, "stance": "bullish", "points": ["方法要点"], "risks": ["方法风险"]},
            ensure_ascii=False,
        ),
        "舆情分析师": json.dumps(
            {"score": 60, "stance": "neutral", "points": ["舆情要点"], "risks": []},
            ensure_ascii=False,
        ),
        "走势分析师": json.dumps(
            {"score": 40, "stance": "bearish", "points": ["走势要点"], "risks": ["走势风险"]},
            ensure_ascii=False,
        ),
        "多空辩论研究员": json.dumps(
            {"bull": "多头理由", "bear": "空头理由"}, ensure_ascii=False
        ),
        "最终决策人": json.dumps(
            {
                "verdict": "看多",
                "score": 85,
                "thesis": "情绪启动+资金确认",
                "risks": ["大盘回调"],
                "action": "回踩低吸",
            },
            ensure_ascii=False,
        ),
    }


def _candidates() -> list[dict]:
    return [
        {
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "industry": "银行",
            "close": 10.5,
            "pct_chg": 2.1,
            "pct_5d": 5.0,
            "pct_20d": 8.0,
            "volume_ratio": 1.5,
            "macd_state": "金叉",
            "money_class": "资金一致确认",
        }
    ]


def _loader(code: str) -> dict:
    return {
        "stock": {"ts_code": code, "name": "平安银行", "industry": "银行"},
        "daily": {"close": 10.5, "pct_chg": 2.1},
        "weekly": {"trend": "周线多头"},
        "moneyflow": {"net_sum_5": 1000},
        "news": [{"title": "利好", "sentiment": "positive"}],
    }


def test_run_judge_full_flow_and_weighted_scoring():
    client = FakeClient(_full_flow_responses())
    progress: list[tuple] = []

    result = run_judge(
        client,
        AgentConfig(),
        as_of="20260802",
        candidates=_candidates(),
        loader=_loader,
        candidates_limit=1,
        depth=1,
        final_count=1,
        on_progress=lambda stage, step, total, msg: progress.append((stage, step, total)),
    )

    # 阶段参数回显
    assert result["as_of"] == "20260802"
    assert result["candidates_limit"] == 1
    assert result["depth"] == 1
    assert result["final_count"] == 1

    # 粗筛保留真实候选
    assert result["coarse"][0]["ts_code"] == "000001.SZ"

    # 加权汇总:0.4*80 + 0.3*60 + 0.3*40 = 62
    deep = result["deep"][0]
    assert deep["score"] == 62.0
    assert deep["stance"] == "bullish"
    assert deep["analysts"]["methodology"]["score"] == 80.0
    assert deep["analysts"]["sentiment"]["score"] == 60.0
    assert deep["analysts"]["trend"]["score"] == 40.0

    # 辩论 + 风控定稿
    final = result["final"][0]
    assert final["ts_code"] == "000001.SZ"
    assert final["verdict"] == "看多"
    assert final["thesis"] == "情绪启动+资金确认"
    assert final["action"] == "回踩低吸"
    assert final["risks"] == ["大盘回调"]
    assert final["debate"] == {"bull": "多头理由", "bear": "空头理由"}
    assert final["rank"] == 1

    # 三阶段进度都报过
    stages = [p[0] for p in progress]
    assert "coarse" in stages and "deep" in stages and "debate" in stages


def test_run_judge_clamps_parameters():
    client = FakeClient(_full_flow_responses())
    result = run_judge(
        client,
        AgentConfig(),
        as_of="20260802",
        candidates=_candidates(),
        loader=_loader,
        candidates_limit=999,
        depth=50,
        final_count=20,
    )
    assert result["candidates_limit"] == 20
    assert result["depth"] == 20
    assert result["final_count"] == 3


def test_run_judge_fails_loudly_on_empty_model_output():
    client = FakeClient({"选股总分析师": ""})
    with pytest.raises(AgentOutputError, match="空内容"):
        run_judge(
            client,
            AgentConfig(),
            as_of="20260802",
            candidates=_candidates(),
            loader=_loader,
            candidates_limit=1,
            depth=1,
            final_count=1,
        )


# ---------------------------------------------------------------- 结果落库幂等


def test_upsert_agent_judgments_idempotent(tmp_path):
    import pandas as pd

    from engine.db import Store

    path = tmp_path / "agents.duckdb"
    df = pd.DataFrame(
        [
            {
                "run_id": "r1",
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "industry": "银行",
                "rank": 1,
                "score": 82.5,
                "stance": "bullish",
                "thesis": "情绪启动",
                "risks": json.dumps(["回撤"], ensure_ascii=False),
                "stage_json": json.dumps({"verdict": "看多"}, ensure_ascii=False),
            }
        ]
    )
    with Store(path, ensure_schema=True) as store:
        first = store.upsert_agent_judgments(df)
        second = store.upsert_agent_judgments(df)
        rows = store.agent_judgments("r1")
    assert first == 1
    assert second == 1
    assert len(rows) == 1
    assert rows.iloc[0]["thesis"] == "情绪启动"


def test_recent_agent_runs_filters_by_as_of(tmp_path):
    from engine.db import Store

    path = tmp_path / "agents.duckdb"
    base = {
        "status": "succeeded",
        "stage": "done",
        "candidates": 1,
        "depth": 1,
        "final_count": 1,
        "progress_json": "{}",
        "created_at": "2026-08-01T00:00:00+00:00",
        "started_at": None,
        "finished_at": "2026-08-01T00:01:00+00:00",
        "heartbeat_at": "2026-08-01T00:01:00+00:00",
        "error_json": None,
        "result_json": "{}",
    }
    with Store(path, ensure_schema=True) as store:
        store.record_agent_run(
            {**base, "run_id": "a", "as_of": "20260801"}
        )
        store.record_agent_run(
            {
                **base,
                "run_id": "b",
                "as_of": "20260802",
                "created_at": "2026-08-02T00:00:00+00:00",
            }
        )
        all_rows = store.recent_agent_runs(limit=10)
        filtered = store.recent_agent_runs(limit=10, as_of="20260802")
    assert list(all_rows["run_id"]) == ["b", "a"]
    assert list(filtered["run_id"]) == ["b"]


__all__: list[str] = []

# ---------------------------------------------------------------- 单只研判


def test_run_single_full_flow():
    client = FakeClient(_full_flow_responses())
    progress: list[tuple] = []
    snapshot = {
        "stock": {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行"},
        "daily": {"close": 10.5, "pct_chg": 2.1},
        "weekly": {"trend": "周线多头"},
        "moneyflow": {"net_sum_5": 1000},
        "news": {
            "source_note": "双源",
            "stock_items": [{"title": "利好", "sentiment": "positive", "relevance": 0.9}],
            "industry_items": [],
        },
    }

    result = run_single(
        client,
        AgentConfig(),
        as_of="20260803",
        snapshot=snapshot,
        on_progress=lambda stage, step, total, msg: progress.append((stage, step, total)),
    )

    assert result["mode"] == "single"
    assert result["final"][0]["ts_code"] == "000001.SZ"
    assert result["final"][0]["rank"] == 1
    stages = [p[0] for p in progress]
    assert "deep" in stages and "debate" in stages and "done" in stages


def test_run_single_rejects_missing_final_score():
    responses = _full_flow_responses()
    responses["最终决策人"] = json.dumps(
        {
            "verdict": "看多",
            "thesis": "情绪启动+资金确认",
            "risks": ["大盘回调"],
            "action": "回踩低吸",
        },
        ensure_ascii=False,
    )

    with pytest.raises(AgentOutputError, match="最终决策人的 score"):
        run_single(
            FakeClient(responses),
            AgentConfig(),
            as_of="20260803",
            snapshot=_loader("000001.SZ"),
        )


def test_run_single_fails_on_empty_snapshot():
    from engine.agents import AgentOutputError
    client = FakeClient(_full_flow_responses())
    with pytest.raises(AgentOutputError, match="快照为空"):
        run_single(client, AgentConfig(), as_of="20260803", snapshot={})
