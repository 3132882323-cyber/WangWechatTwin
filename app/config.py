from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from app.models import ContactProfile


class OpenAISettings(BaseModel):
    deepseek_drafts: bool=False
    deepseek_fast: bool=False
    provider: Literal["auto", "api", "account", "web", "deepseek_web", "doubao_web", "hybrid_web"] = "auto"
    web_reply_timeout_seconds: float = 180
    primary_model: str = "gpt-6-luna"
    high_risk_model: str = "gpt-6.1-sol"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 600


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
