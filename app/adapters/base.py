from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import IncomingMessage


class MessageAdapter(ABC):
    @abstractmethod
    def doctor(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def poll(self) -> list[IncomingMessage]:
        raise NotImplementedError

    @abstractmethod
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        return None
