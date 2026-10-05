from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timezone

from app.adapters.base import MessageAdapter
from app.models import IncomingMessage


class MockAdapter(MessageAdapter):
    def __init__(self, messages: list[dict] | None = None):
        self.queue = deque(messages or [])
        self.sent: list[tuple[str, str]] = []

    def doctor(self) -> dict:
        return {"ok": True, "adapter": "mock", "queued": len(self.queue)}

    def poll(self) -> list[IncomingMessage]:
        if not self.queue:
            return []
        raw = self.queue.popleft()
        contact = str(raw.get("contact", "测试联系人"))
        content = str(raw.get("content", ""))
        digest = hashlib.sha256(
            f"{contact}|{content}|{len(self.sent)}|{len(self.queue)}".encode("utf-8")
        ).hexdigest()[:24]
        return [
            IncomingMessage(
                external_id=f"mock:{digest}",
                contact=contact,
                sender=str(raw.get("sender", contact)),
                content=content,
                message_type=str(raw.get("message_type", "text")),
                chat_type=str(raw.get("chat_type", "friend")),
                received_at=datetime.now(timezone.utc),
            )
        ]

    def send_text(self, contact: str, text: str) -> bool:
        self.sent.append((contact, text))
        return True
