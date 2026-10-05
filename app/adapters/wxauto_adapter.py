from __future__ import annotations

import hashlib
import platform
import time
from datetime import datetime, timezone
from typing import Any

from app.adapters.base import MessageAdapter
from app.config import AppConfig
from app.models import IncomingMessage


class WxAutoUnavailable(RuntimeError):
    pass


class WxAutoAdapter(MessageAdapter):
    """Thin compatibility layer over wxautox4/wxauto4.

    Only visible Windows UI automation is used. This adapter does not bypass login,
    inject DLLs, read process memory, or evade platform controls.
    """

    def __init__(self, config: AppConfig, initialize: bool = True):
        self.config = config
        self.variant = "unknown"
        self.wx: Any = None
        self._free_baselines: dict[str, set[str]] = {}
        if initialize:
            self._initialize()

    def _initialize(self) -> None:
        if platform.system() != "Windows":
            raise WxAutoUnavailable("wxauto 只能在原生 Windows 上运行")
        errors: list[str] = []
        wechat_cls = None
        try:
            from wxautox4 import WeChat as PlusWeChat  # type: ignore

            wechat_cls = PlusWeChat
            self.variant = "wxautox4-plus"
        except Exception as exc:
            errors.append(f"wxautox4: {exc}")
        if wechat_cls is None:
            try:
                from wxauto4 import WeChat as FreeWeChat  # type: ignore

                wechat_cls = FreeWeChat
                self.variant = "wxauto4-free"
            except Exception as exc:
                errors.append(f"wxauto4: {exc}")
        if wechat_cls is None:
            raise WxAutoUnavailable("未找到微信自动化组件；" + " | ".join(errors))
        if self.variant == "wxauto4-free":
            from wxauto4 import WxParam
            WxParam.TELEMETRY_ENABLED = False
            WxParam.ENABLE_FILE_LOGGER = False
        try:
            self.wx = wechat_cls(debug=False, resize=False, ads=False)
        except TypeError:
            self.wx = wechat_cls()
        except Exception as exc:
            raise WxAutoUnavailable(f"初始化微信窗口失败：{exc}") from exc

    def doctor(self) -> dict:
        result: dict[str, Any] = {
            "ok": self.wx is not None,
            "adapter": "wxauto",
            "variant": self.variant,
            "platform": platform.platform(),
            "features": {},
        }
        if self.wx is None:
            return result
        feature_names = [
            "GetNextNewMessage",
            "GetAllMessage",
            "GetSession",
            "ChatWith",
            "ChatInfo",
            "SendMsg",
            "IsOnline",
        ]
        for name in feature_names:
            result["features"][name] = callable(getattr(self.wx, name, None))

        global_listener = result["features"]["GetNextNewMessage"]
        configured_scan = all(
            result["features"].get(name, False)
            for name in ("GetAllMessage", "ChatWith", "SendMsg")
        )
        result["listen_mode"] = (
            "global_listener" if global_listener else "configured_contact_scan" if configured_scan else "unavailable"
        )
        result["ok"] = bool(result["ok"] and result["features"]["SendMsg"] and (global_listener or configured_scan))
        if configured_scan and not global_listener:
            result["warning"] = (
                "当前使用免费版兼容扫描：只轮询 config.yaml 中明确列出的联系人，"
                "首次扫描只建立历史基线，不处理旧消息；联系人较多时建议使用支持全局监听的版本。"
            )
        if hasattr(self.wx, "IsOnline"):
            try:
                result["online"] = bool(self.wx.IsOnline())
            except Exception as exc:
                result["online"] = None
                result["online_error"] = str(exc)
        try:
            info = self.wx.ChatInfo() if hasattr(self.wx, "ChatInfo") else None
            result["current_chat"] = info
        except Exception as exc:
            result["current_chat_error"] = str(exc)
        return result

    @staticmethod
    def _attr(obj: Any, name: str, default: Any = None) -> Any:
        try:
            return getattr(obj, name, default)
        except Exception:
            return default

    def _normalize_container(self, raw: Any) -> list[tuple[str, str, list[Any]]]:
        conversations: list[tuple[str, str, list[Any]]] = []
        if not raw:
            return conversations
        if isinstance(raw, dict) and "chat_name" in raw:
            conversations.append(
                (
                    str(raw.get("chat_name", "")),
                    str(raw.get("chat_type", "friend")),
                    list(raw.get("msg") or raw.get("messages") or []),
                )
            )
            return conversations
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    conversations.append(
                        (
                            str(value.get("chat_name") or key),
                            str(value.get("chat_type", "friend")),
                            list(value.get("msg") or value.get("messages") or []),
                        )
                    )
                elif isinstance(value, (list, tuple)):
                    conversations.append((str(key), "friend", list(value)))
            return conversations
        if isinstance(raw, (list, tuple)):
            conversations.append(("", "friend", list(raw)))
        return conversations

    def _message_id(self, contact: str, msg: Any, content: str) -> str:
        candidates = [
            self._attr(msg, "id"),
            self._attr(msg, "msg_id"),
            self._attr(msg, "ui_id"),
            self._attr(msg, "control_id"),
        ]
        for candidate in candidates:
            if candidate not in (None, ""):
                return f"wx:{contact}:{candidate}"
        raw = f"{contact}|{self._attr(msg, 'sender', '')}|{content}|{repr(msg)}"
        return "wx:" + hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    def _to_incoming(self, contact: str, chat_type: str, messages: list[Any]) -> list[IncomingMessage]:
        result: list[IncomingMessage] = []
        for msg in messages:
            attr = str(self._attr(msg, "attr", "")).lower()
            msg_type = str(self._attr(msg, "type", "text")).lower()
            # Never treat our own or system messages as inbound.
            if attr and attr not in {"friend", "other"}:
                continue
            if msg_type not in {"text", "quote", "voice"}:
                continue
            content = str(self._attr(msg, "content", "") or "").strip()
            if not content:
                continue
            resolved_contact = contact or str(self._attr(msg, "chat_name", "")).strip()
            if not resolved_contact:
                continue
            sender = str(self._attr(msg, "sender", resolved_contact) or resolved_contact)
            result.append(
                IncomingMessage(
                    external_id=self._message_id(resolved_contact, msg, content),
                    contact=resolved_contact,
                    sender=sender,
                    content=content,
                    message_type=msg_type,
                    chat_type=chat_type or "friend",
                    received_at=datetime.now(timezone.utc),
                    raw_summary=f"{type(msg).__name__}:{attr}:{msg_type}",
                )
            )
        return result

    def _chat_with(self, contact: str) -> None:
        try:
            self.wx.ChatWith(who=contact, exact=self.config.wechat.exact_contact_match)
        except TypeError:
            self.wx.ChatWith(contact)
        time.sleep(0.18)
        if hasattr(self.wx, "ChatInfo"):
            info = self.wx.ChatInfo() or {}
            actual = str(info.get("chat_name", "")) if isinstance(info, dict) else ""
            if actual and actual != contact:
                raise WxAutoUnavailable(f"联系人校验失败：目标={contact}，当前窗口={actual}")

    def _poll_configured_contacts(self) -> list[IncomingMessage]:
        """Free-edition fallback using ChatWith + GetAllMessage.

        It scans only explicitly configured contacts. The first scan creates a baseline and
        deliberately ignores old history, preventing an initial reply storm.
        """
        contacts = [item.name for item in self.config.contacts if item.mode != "off"]
        if not contacts:
            return []
        result: list[IncomingMessage] = []
        for contact in contacts:
            try:
                self._chat_with(contact)
                raw = self.wx.GetAllMessage()
            except Exception as exc:
                raise WxAutoUnavailable(f"扫描联系人“{contact}”失败：{exc}") from exc
            if not isinstance(raw, (list, tuple)):
                raise WxAutoUnavailable(f"联系人“{contact}”返回的消息列表格式异常")
            messages = list(raw)
            keyed: list[tuple[str, Any]] = []
            for msg in messages[-200:]:
                content = str(self._attr(msg, "content", "") or "").strip()
                keyed.append((self._message_id(contact, msg, content), msg))
            current_ids = {key for key, _ in keyed}
            previous = self._free_baselines.get(contact)
            self._free_baselines[contact] = current_ids
            if previous is None:
                continue
            new_messages = [msg for key, msg in keyed if key not in previous]
            chat_type = "friend"
            if hasattr(self.wx, "ChatInfo"):
                try:
                    info = self.wx.ChatInfo() or {}
                    if isinstance(info, dict):
                        chat_type = str(info.get("chat_type") or "friend")
                except Exception:
                    pass
            result.extend(self._to_incoming(contact, chat_type, new_messages))
        return result

    def poll(self) -> list[IncomingMessage]:
        if self.wx is None:
            raise WxAutoUnavailable("微信适配器尚未初始化")
        # Prefer bounded scans over a global listener, so unlisted chats stay unread.
        if all(hasattr(self.wx, name) for name in ("ChatWith", "GetAllMessage", "SendMsg")):
            return self._poll_configured_contacts()
        if hasattr(self.wx, "GetNextNewMessage"):
            try:
                raw = self.wx.GetNextNewMessage(filter_mute=self.config.wechat.filter_mute)
            except TypeError:
                raw = self.wx.GetNextNewMessage()
            except Exception as exc:
                raise WxAutoUnavailable(f"读取微信新消息失败：{exc}") from exc
            result: list[IncomingMessage] = []
            for contact, chat_type, messages in self._normalize_container(raw):
                result.extend(self._to_incoming(contact, chat_type, messages))
            return result
        if all(hasattr(self.wx, name) for name in ("ChatWith", "GetAllMessage", "SendMsg")):
            return self._poll_configured_contacts()
        raise WxAutoUnavailable(
            "当前 wxauto 版本既没有 GetNextNewMessage，也缺少 ChatWith/GetAllMessage，无法监听消息。"
        )

    def send_text(self, contact: str, text: str) -> bool:
        if self.wx is None:
            raise WxAutoUnavailable("微信适配器尚未初始化")
        if not text.strip():
            return False
        try:
            if hasattr(self.wx, "ChatWith"):
                self._chat_with(contact)
            try:
                response = self.wx.SendMsg(
                    msg=text,
                    who=contact,
                    clear=True,
                    exact=self.config.wechat.exact_contact_match,
                )
            except TypeError:
                try:
                    response = self.wx.SendMsg(msg=text)
                except TypeError:
                    response = self.wx.SendMsg(text, contact)
        except Exception as exc:
            raise WxAutoUnavailable(f"发送给“{contact}”失败：{exc}") from exc

        if response is None:
            return True
        if isinstance(response, bool):
            return response
        for name in ("is_success", "success", "ok"):
            value = self._attr(response, name)
            if isinstance(value, bool):
                return value
        return True
