"""UI 设置接口:读写 config/settings.local.yaml(OpenAI 兼容接口与研判参数)。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.settings_store import (
    read_ui_values,
    save_local,
    test_api_key_available,
)

router = APIRouter()

_SETTING_KEYS = {
    "agent", "ai", "news",
}


class AgentSettingsIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=100, le=32000)
    default_candidates: int | None = Field(default=None, ge=1, le=500)
    default_depth: int | None = Field(default=None, ge=1, le=50)
    default_final: int | None = Field(default=None, ge=1, le=50)
    max_candidates: int | None = None
    max_depth: int | None = None
    max_final: int | None = None


class SettingsIn(BaseModel):
    agent: AgentSettingsIn | None = None


class SettingsPatch(BaseModel):
    agent: dict | None = None
    ai: dict | None = None
    news: dict | None = None


@router.get("/settings")
def get_settings() -> dict:
    """读取当前有效选项(已合并本地覆盖),api_key 只返回环境变量名。"""
    values = read_ui_values()
    env = values.get("agent", {}).get("api_key_env") or "WORKBENCH_AI_API_KEY"
    values["api_key_available"] = test_api_key_available(env)
    values["api_key_hint"] = "配置只存环境变量名,不落明文密钥"
    return values


@router.put("/settings")
def put_settings(body: SettingsPatch) -> dict:
    """把 UI 提交的设置保存到 settings.local.yaml(白名单键)。"""
    payload = body.model_dump(exclude_none=True)
    if "agent" in payload:
        payload["agent"] = {k: v for k, v in payload["agent"].items() if v is not None}
    if "ai" in payload:
        payload["ai"] = {k: v for k, v in payload["ai"].items() if v is not None}
    if "news" in payload:
        payload["news"] = {k: v for k, v in payload["news"].items() if v is not None}
    saved = save_local(payload)
    return {"saved": saved, "message": "设置已保存,重启服务后 AI 配置生效"}
