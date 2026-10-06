from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from app.models import ContactProfile


class OpenAISettings(BaseModel):
    provider: Literal["auto", "api", "account", "web"] = "auto"
    web_reply_timeout_seconds: float = 180
    primary_model: str = "gpt-6-luna"
    high_risk_model: str = "gpt-6.1-sol"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 600


class WeChatSettings(BaseModel):
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


class WebSettings(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


class PathSettings(BaseModel):
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
    owner_name: str = "使用者"
    owner_alias: str = "本人"
    adapter: Literal["wxauto", "mock", "history_readonly", "history_verified_sender"] = "wxauto"
    mode: Literal["shadow", "low_risk_auto", "full_auto", "off"] = "shadow"
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    wechat: WeChatSettings = Field(default_factory=WeChatSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
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
