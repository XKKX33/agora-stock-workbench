"""UI 设置持久化:读写 config/settings.local.yaml。

只覆盖 settings.yaml 的 agent/ai/news 等键,不重写整个文件。
密钥字段(api_key)只允许写环境变量名,不落明文;设置页面可填环境变量名
指向一个已存在的变量,绝不把真实密钥写进仓库。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from engine.config import CONFIG_DIR, load_settings_with_local

LOCAL_FILE = CONFIG_DIR / "settings.local.yaml"

_AGENT_KEYS = {
    "enabled", "provider", "model", "base_url", "api_key_env",
    "temperature", "max_tokens", "reasoning_effort",
    "default_candidates", "default_depth", "default_final",
    "max_candidates", "max_depth", "max_final",
}
_AI_KEYS = {
    "enabled", "provider", "model", "base_url", "api_key_env",
    "temperature", "max_tokens", "reasoning_effort",
}
_NEWS_KEYS = {"enabled", "half_life_days", "close_cutoff"}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings_with_override() -> dict:
    """加载 settings.yaml,并用 settings.local.yaml 覆盖。"""
    return load_settings_with_local()


def load_local() -> dict:
    if not LOCAL_FILE.exists():
        return {}
    with open(LOCAL_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_local(patch: dict) -> dict:
    """把 UI 提交的设置 patch 合并进 settings.local.yaml,返回最终 local 配置。"""
    current = load_local()
    merged = _deep_merge(current, patch or {})
    keep = {}
    for section, keys in (("agent", _AGENT_KEYS), ("ai", _AI_KEYS), ("news", _NEWS_KEYS)):
        raw = merged.get(section)
        if not isinstance(raw, dict):
            continue
        clean = {k: v for k, v in raw.items() if k in keys and v is not None}
        if "api_key_env" in clean:
            # Provider credentials are intentionally fixed to the single supported
            # environment variable; never persist arbitrary secret-bearing names.
            clean["api_key_env"] = "WORKBENCH_AI_API_KEY"
        if clean:
            keep[section] = clean
    LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_FILE.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(keep, allow_unicode=True, sort_keys=False), encoding="utf-8")
    shutil.move(str(tmp), str(LOCAL_FILE))
    return keep


def read_ui_values() -> dict:
    """给设置页面用的脱敏值: api_key 只返回环境变量名, 不返回密钥。"""
    settings = load_settings_with_override()
    agent = settings.get("agent") or {}
    ai = settings.get("ai") or {}
    news = settings.get("news") or {}
    return {
        "agent": {k: agent.get(k) for k in sorted(_AGENT_KEYS)},
        "ai": {k: ai.get(k) for k in sorted(_AI_KEYS)},
        "news": {
            "enabled": news.get("enabled"),
            "half_life_days": news.get("half_life_days"),
            "close_cutoff": news.get("close_cutoff"),
        },
        "local_file": str(LOCAL_FILE),
    }


def test_api_key_available(api_key_env: str) -> bool:
    """检查某个环境变量名下是否已有密钥(不回显)。"""
    return bool(api_key_env and os.environ.get(api_key_env))


__all__ = [
    "LOCAL_FILE", "load_settings_with_override", "load_local",
    "save_local", "read_ui_values", "test_api_key_available",
]
