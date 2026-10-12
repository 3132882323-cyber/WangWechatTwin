from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from app.models import ContactProfile


class LocalProxySettings(BaseModel):
    enabled: bool = False
    base_url: str
    api_key_file: str
    session_db: str = ".runtime/local_proxy/sessions.sqlite3"
    timeout_seconds: float = Field(default=45, ge=1, le=120)

    @model_validator(mode="after")
    def require_loopback_proxy(self):
        if self.base_url not in {"http://127.0.0.1:19655/v1", "http://127.0.0.1:19656/v1"}:
            raise ValueError("local proxy must use its fixed loopback endpoint")
        if not self.api_key_file.strip():
            raise ValueError("local proxy requires a private API key file")
        return self


class OpenAISettings(BaseModel):
    local_proxies: dict[Literal["deepseek", "doubao"], LocalProxySettings] = Field(default_factory=dict)
    everyday_lane: Literal["deepseek", "doubao"] = "deepseek"
    disabled_lanes: list[Literal["chatgpt", "deepseek", "doubao"]] = Field(default_factory=list)
    deepseek_drafts: bool=False
    deepseek_fast: bool=False
    provider: Literal["auto", "api", "account", "web", "deepseek_web", "doubao_web", "hybrid_web"] = "auto"
    web_reply_timeout_seconds: float = 180
    primary_model: str = "gpt-6-luna"
    high_risk_model: str = "gpt-6.1-sol"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 600

    @model_validator(mode="after")
    def validate_proxy_lanes(self):
        if self.provider == "hybrid_web" and self.everyday_lane in self.disabled_lanes:
            raise ValueError("everyday lane cannot be disabled")
        provider_lane = {"web": "chatgpt", "deepseek_web": "deepseek", "doubao_web": "doubao"}.get(self.provider)
        if provider_lane in self.disabled_lanes:
            raise ValueError("configured provider uses a disabled model lane")
        if "deepseek" in self.disabled_lanes and self.deepseek_drafts:
            raise ValueError("disabled DeepSeek cannot generate comparison drafts")
        session_files = {item.session_db for item in self.local_proxies.values() if item.enabled}
        if len(session_files) > 1:
            raise ValueError("enabled local proxy lanes must share their session database")
        for lane, settings in self.local_proxies.items():
            if lane in self.disabled_lanes and settings.enabled:
                raise ValueError("disabled lane cannot have an enabled local proxy")
            expected = "http://127.0.0.1:" + ("19655" if lane == "deepseek" else "19656") + "/v1"
            if settings.base_url != expected:
                raise ValueError("local proxy endpoint does not match its lane")
        if any(item.enabled for item in self.local_proxies.values()) and self.provider != "hybrid_web":
            raise ValueError("local proxy lanes require hybrid_web routing")
        return self


class WeChatSettings(BaseModel):
    use_greeting_cache: bool=True
    sender_all_existing_chats: bool = False
    group_only_mentions: bool = True
    ignore_simple_acknowledgments: bool = False
    sender_allowed_contacts: list[str] = Field(default_factory=list)
    poll_seconds: float = 2.0
    allow_groups: bool = False
    allow_unknown_contacts: bool = False
    unknown_contact_mode: Literal["shadow", "low_risk_auto", "full_auto", "off"] = "shadow"
    exact_contact_match: bool = True
    filter_mute: bool = False
    max_auto_sends_per_hour: int = 30
    max_reply_chars: int = 220
    send_holding_on_review: bool = True


class MemorySettings(BaseModel):
    recent_messages: int = 24
    max_contact_memories: int = 30
    retain_message_days: int = 30


class ProactiveSettings(BaseModel):
    enabled: bool = False
    review_only: Literal[True] = True
    all_existing_contacts: bool = True
    contacts: list[str] = Field(default_factory=list)
    purpose: Literal["followup", "chat", "both"] = "both"
    min_idle_hours: float = Field(default=24, ge=1)
    cooldown_hours: float = Field(default=72, ge=24)
    max_drafts_per_day: int = Field(default=3, ge=1, le=20)
    active_start_hour: int = Field(default=10, ge=0, le=23)
    active_end_hour: int = Field(default=20, ge=1, le=24)
    interval_seconds: int = Field(default=300, ge=30)


class StickerSettings(BaseModel):
    enabled: bool = False
    catalog: str = ".runtime/history_reader/sticker_catalog.json"
    max_per_contact_per_day: int = Field(default=3, ge=1, le=10)
    port: int = Field(default=30004,ge=1,le=65535)
    bootstrap_manifest: str = '.runtime/local_api/native_media/manifest.json'


class MediaSettings(BaseModel):
    voice_enabled: bool=False
    images_enabled: bool=False
    voice_model: str='.runtime/models/whisper-small-local'
    max_voice_seconds: int=120
    max_pending_voice: int=20


class LocalAPISettings(BaseModel):
    auto_load: bool = False
    bootstrap_manifest: str = ".runtime/local_api/v2/manifest.json"
    # This connector never falls back to GUI sending.
    port: int = Field(default=30001, ge=1, le=65535)
    token_file: str = ".runtime/local_api/token.txt"
    timeout_seconds: float = Field(default=8, gt=0, le=60)
    receipt_timeout_seconds: float = Field(default=12, gt=0, le=60)


class WebSettings(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


class PathSettings(BaseModel):
    personal_database: str='.runtime/personal_memory.sqlite3'
    role_profile: str = '.runtime/history_reader/role_profile.json'
    chat_memory: str = ".runtime/history_reader/chat_memory.sqlite3"
    browser_bridge: str = ".runtime/browser_bridge"
    style_history: str = ".runtime/history_reader/style_samples.sqlite3"
    history_reader: str = ".runtime/history_reader"
    learned_style: str = "data/learned_style_summary.json"
    database: str = ".runtime/wang_wechat_twin.sqlite3"
    persona: str = "data/persona.md"
    business_rules: str = "data/business_rules.md"
    reply_samples: str = "data/reply_samples.csv"
    pause_file: str = ".runtime/PAUSE"


class AppConfig(BaseModel):
    owner_identity_exclusions: list[str] = Field(default_factory=list)
    owner_name: str = "夏鑫鑫"
    owner_alias: str = "本人"
    adapter: Literal["wxauto", "mock", "history_readonly", "history_verified_sender", "history_http_sender"] = "wxauto"
    mode: Literal["shadow", "low_risk_auto", "full_auto", "off"] = "shadow"
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    wechat: WeChatSettings = Field(default_factory=WeChatSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)
    stickers: StickerSettings = Field(default_factory=StickerSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    local_api: LocalAPISettings = Field(default_factory=LocalAPISettings)
    web: WebSettings = Field(default_factory=WebSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    contacts: list[ContactProfile] = Field(default_factory=list)
    project_root: Path = Field(exclude=True)

    @model_validator(mode="after")
    def validate_contacts(self) -> "AppConfig":
        seen: set[str] = set()
        for contact in self.contacts:
            key = contact.name.strip()
            if not key:
                raise ValueError("联系人名称不能为空")
            if key in seen:
                raise ValueError(f"联系人重复：{key}")
            seen.add(key)
        return self

    def resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def contact_map(self) -> dict[str, ContactProfile]:
        return {item.name: item for item in self.contacts}


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    load_dotenv(path.parent / ".env", override=False)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["project_root"] = path.parent
    config = AppConfig.model_validate(data)
    config.resolve(config.paths.database).parent.mkdir(parents=True, exist_ok=True)
    config.resolve(config.paths.pause_file).parent.mkdir(parents=True, exist_ok=True)
    return config


def openai_credentials() -> tuple[str | None, str | None]:
    return os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_BASE_URL") or None
