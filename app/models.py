from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.low: 0,
    RiskLevel.medium: 1,
    RiskLevel.high: 2,
    RiskLevel.critical: 3,
}


class IncomingMessage(BaseModel):
    sender_key: str = ''
    media_paths: list[str] = Field(default_factory=list)
    display_name: str | None = None
    external_id: str
    contact: str
    sender: str
    content: str
    message_type: str = "text"
    chat_type: str = "friend"
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_summary: str = ""


class RiskAssessment(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    safe_holding_reply: str | None = None


class ReplyDecision(BaseModel):
    media_description: str = ''
    media_confidence: float = Field(default=0,ge=0,le=1)
    action: Literal["send", "hold", "review", "ignore"]
    risk: RiskLevel
    reply: str = ""
    holding_reply: str | None = None
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    facts_to_confirm: list[str] = Field(default_factory=list)
    memory_updates: list[str] = Field(default_factory=list)


class ContactProfile(BaseModel):
    name: str
    relationship: str = "business"
    domain: str = "general"
    tone: str = "自然直接"
    mode: Literal["inherit", "shadow", "low_risk_auto", "full_auto", "off"] = "inherit"
    notes: str = ""


class DraftRecord(BaseModel):
    id: int
    contact: str
    inbound_message_id: int | None
    incoming_content: str | None = None
    reply: str
    edited_reply: str | None = None
    holding_reply: str | None = None
    action: str
    risk: str
    reason: str
    confidence: float
    status: str
    created_at: str
    updated_at: str
