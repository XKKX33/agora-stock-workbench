"""配置加载:settings.yaml + strategies/<name>.yaml,并做键归一。

- 路径一律相对 workbench/ 根目录解析。
- 资金 overlay 的键做规范化(下划线/逗号变体统一),与 classify_money 输出对齐。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

# workbench 根目录 = 本文件上两级 (engine/config.py -> engine -> workbench)
WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = WORKBENCH_ROOT / "config"

# 资金分层规范键(classify_money 的权威输出)
MONEY_CLASSES = (
    "资金一致确认",
    "大资金承接型强分歧",
    "总资金认可但大单不连续",
    "资金同步分歧，降级",
    "资金未充分确认",
)

# 常见书写变体 -> 规范键
_MONEY_ALIASES = {
    "资金同步分歧_降级": "资金同步分歧，降级",
    "资金同步分歧,降级": "资金同步分歧，降级",
}


def load_workspace_env() -> None:
    """加载工作台根目录的 .env，且不覆盖显式进程环境变量。"""
    load_dotenv(WORKBENCH_ROOT / ".env", override=False)


def _read_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Dict[str, Any]:
    return _read_yaml(CONFIG_DIR / "settings.yaml")


def load_settings_with_local() -> Dict[str, Any]:
    """加载 settings.yaml,并用 config/settings.local.yaml 覆盖(UI 设置页面写入)。"""
    settings = load_settings()
    local_path = CONFIG_DIR / "settings.local.yaml"
    if not local_path.exists():
        return settings
    local = _read_yaml(local_path)
    if not isinstance(local, dict):
        return settings
    out = dict(settings)
    for section, values in local.items():
        if not isinstance(values, dict):
            continue
        base = out.get(section)
        if isinstance(base, dict):
            merged = dict(base)
            merged.update(values)
            out[section] = merged
        else:
            out[section] = values
    return out


def _normalize_money_overlay(overlay: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, v in (overlay or {}).items():
        key = _MONEY_ALIASES.get(k, k)
        out[key] = float(v)
    return out


def load_strategy(name: str) -> Dict[str, Any]:
    """加载策略并展平为打分层需要的结构。"""
    raw = _read_yaml(CONFIG_DIR / "strategies" / f"{name}.yaml")
    raw["money_overlay"] = _normalize_money_overlay(raw.get("money_overlay", {}))
    raw.setdefault("strategy_name", name)
    return raw


def resolve_path(rel: str) -> Path:
    """把配置中相对路径解析为绝对路径(基于 workbench 根)。"""
    p = Path(rel)
    return p if p.is_absolute() else (WORKBENCH_ROOT / p)


def tushare_token(settings: Dict[str, Any]) -> str | None:
    """按 settings.tushare.token_env 读环境变量;找不到回退 None。"""
    env_key = (settings.get("tushare", {}) or {}).get("token_env", "TUSHARE_TOKEN")
    return os.environ.get(env_key)
