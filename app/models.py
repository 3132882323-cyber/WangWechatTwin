from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class ReplyCandidate(BaseModel):
    """An internal draft with subjective ordinal scores, not probabilities."""

    label: Literal["A", "B", "C"]
    reply: str = Field(min_length=1, max_length=600)
    style_match: int = Field(
        ge=0, le=5, strict=True,
        description="Subjective 0..5 owner-style match; not a calibrated probability.",
    )
    context_fit: int = Field(
        ge=0, le=5, strict=True,
        description="Subjective 0..5 fit to the concrete incoming message and context.",
    )
    continuation: int = Field(
        ge=0, le=5, strict=True,
        description="Subjective 0..5 natural continuation when appropriate; respect a closing cue.",
    )
    boundary_respect: int = Field(
        ge=0, le=5, strict=True,
        description="Subjective 0..5 respect for facts, consent, commitments and transparency.",
    )
    invented_facts: bool = Field(
        strict=True,
        description="Whether this draft invents owner experiences, facts or commitments.",
    )
    risk: RiskLevel

    @field_validator("reply")
    @classmethod
    def reply_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate reply must not be blank")
        return value


class ReplyDecision(BaseModel):
    sticker_id: str = ""
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
    candidates: list[ReplyCandidate] | None = Field(default=None, max_length=3)
    selected_candidate: Literal["A", "B", "C"] | None = None

    @model_validator(mode="after")
    def validate_candidate_selection(self) -> ReplyDecision:
        labels = [candidate.label for candidate in self.candidates or []]
        if len(labels) != len(set(labels)):
            raise ValueError("candidate labels must be unique")
        if self.selected_candidate is not None and self.selected_candidate not in labels:
            raise ValueError("selected_candidate must identify a provided candidate")
        return self


class ContactProfile(BaseModel):
    name: str
    relationship: str = "business"
    domain: str = "general"
    tone: str = "自然直接"
    mode: Literal["inherit", "shadow", "low_risk_auto", "full_auto", "off"] = "inherit"
    notes: str = ""


class DraftRecord(BaseModel):
    media_description: str=''
    media_confidence: float=0
    incoming_media_paths: list[str]=Field(default_factory=list)
    original_type: str=''
    kind: str = "reply"
    sticker_id: str = ""
    source_context_ts: int | None = None
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
