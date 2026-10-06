from __future__ import annotations

from dataclasses import dataclass
import re

from app.adapters.base import MessageAdapter
from app.config import AppConfig
from app.db import Database
from app.llm import LLMError, ReplyLLM
from app.models import (
    ContactProfile,
    IncomingMessage,
    RISK_ORDER,
    ReplyDecision,
    RiskLevel,
)
from app.prompts import PromptBuilder
from app.risk import assess_risk


@dataclass
class ProcessResult:
    status: str
    decision: ReplyDecision | None = None
    draft_id: int | None = None


class ReplyPipeline:
    def __init__(self, config: AppConfig, db: Database, llm: ReplyLLM | None = None):
        self.config = config
        self.db = db
        self.prompt_builder = PromptBuilder(config)
        self.llm = llm
        self.contact_map = config.contact_map()

    def _contact(self, name: str) -> ContactProfile | None:
        return self.contact_map.get(name)

    def _mode(self, contact: ContactProfile | None) -> str:
        if self.config.mode in {"shadow", "off"}:
            return self.config.mode
        if contact is None:
            return (
                self.config.wechat.unknown_contact_mode
                if self.config.wechat.allow_unknown_contacts
                else "shadow"
            )
        return self.config.mode if contact.mode == "inherit" else contact.mode

    @staticmethod
    def _max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
        return a if RISK_ORDER[a] >= RISK_ORDER[b] else b

    def _fallback(self, risk: RiskLevel, holding: str | None, error: str) -> ReplyDecision:
        if risk == RiskLevel.low:
            return ReplyDecision(
                action="review",
                risk=risk,
                reply="收到，我看一下，确认后给你回。",
                reason=f"模型不可用，已转人工：{error}",
                confidence=0.0,
            )
        return ReplyDecision(
            action="hold",
            risk=risk,
            reply=holding or "收到，我先核对一下，确认后给你回。",
            holding_reply=holding or "收到，我先核对一下，确认后给你回。",
            reason=f"模型不可用，仅允许安全占位回复：{error}",
            confidence=0.0,
            facts_to_confirm=["模型未生成正式回复，需要本人处理"],
        )

    def _apply_policy(
        self,
        decision: ReplyDecision,
        deterministic_risk: RiskLevel,
        mode: str,
        holding: str | None,
    ) -> ReplyDecision:
        decision.risk = self._max_risk(decision.risk, deterministic_risk)
        decision.reply = " ".join(decision.reply.strip().split())
        if not decision.reply and decision.action != "ignore":
            decision.reply = holding or "收到，我先核一下具体情况，确认完给你回。"
            decision.action = "review"
            decision.reason = (decision.reason + "；模型未给出有效回复").strip("；")
        if decision.holding_reply:
            decision.holding_reply = " ".join(decision.holding_reply.strip().split())

        if len(decision.reply) > self.config.wechat.max_reply_chars:
            decision.action = "review"
            decision.reason = (decision.reason + "；回复过长，转人工审核").strip("；")

        if mode == "off":
            decision.action = "ignore"
            return decision
        if mode == "shadow":
            if decision.action != "ignore":
                decision.action = "review"
            return decision

        if mode=='low_risk_auto' and decision.action=='review' and decision.risk==RiskLevel.low and decision.confidence>=.85 and not decision.facts_to_confirm:
            from app.social_rules import safe_social_reply
            if safe_social_reply(decision.reply):
                decision.action='send'
                decision.reason+='；仅为低风险确认或追问，不作新的本人决定'

        if decision.facts_to_confirm and decision.action == "send":
            decision.action = "hold" if self.config.wechat.send_holding_on_review else "review"

        if mode == "low_risk_auto":
            if decision.risk != RiskLevel.low or decision.confidence < 0.72:
                decision.action = "hold" if self.config.wechat.send_holding_on_review else "review"
        elif mode == "full_auto":
            if decision.risk in {RiskLevel.high, RiskLevel.critical}:
                decision.action = "hold" if self.config.wechat.send_holding_on_review else "review"
            elif decision.confidence < 0.62:
                decision.action = "review"

        # Never trust model-written holding text for a deterministic medium/high/critical case.
        # The only text allowed to go out before review is the fixed safe template.
        if decision.action == "hold":
            if deterministic_risk in {RiskLevel.medium, RiskLevel.high, RiskLevel.critical}:
                decision.holding_reply = holding or "收到，我先核一下具体情况，确认完给你回。"
            elif not decision.holding_reply:
                decision.holding_reply = holding or "收到，我先核一下具体情况，确认完给你回。"
        return decision

    def process(self, message: IncomingMessage, adapter: MessageAdapter) -> ProcessResult:
        if self.db.seen(message.external_id):
            return ProcessResult(status="duplicate")
        if message.chat_type == "group" and not self.config.wechat.allow_groups:
            self.db.add_incoming(message, risk="ignored_group")
            self.db.add_event("ignored_group", "群聊未开启自动处理", contact=message.contact)
            return ProcessResult(status="ignored_group")

        contact = self._contact(message.contact)
        if contact is None and not self.config.wechat.allow_unknown_contacts:
            contact = ContactProfile(
                name=message.contact,
                relationship="未知联系人",
                domain="general",
                mode="shadow",
                notes="尚未加入授权联系人列表，只生成草稿。",
            )
        elif contact is None:
            contact = ContactProfile(
                name=message.contact,
                relationship="本人已有聊天（已授权日常自动回复）" if self.config.wechat.sender_all_existing_chats else "未知联系人",
                domain="general",
                mode=self.config.wechat.unknown_contact_mode,
                notes=("用户已授权接管全部已有聊天，日常低风险消息可直接回复；不得仅因未手工配置联系人而要求审核。"
                       "重要事项和缺乏事实依据的承诺仍须审核。" if self.config.wechat.sender_all_existing_chats
                       else "自动发现的联系人，回复需更谨慎。"),
            )

        risk = assess_risk(message.content)
        inbound_id = self.db.add_incoming(message, risk=risk.level.value)
        vision_ready=(self.db.get_state('browser_bridge') or {}).get('build') in {'sticker-vision-v1','fast-sticker-v2'}
        recognized_sticker=message.message_type=='sticker' and 0<len(message.media_paths)<=3 and self.config.openai.provider=='web' and vision_ready
        if message.message_type != "text" and not recognized_sticker:
            decision = ReplyDecision(action="review", risk=RiskLevel.medium, reply="",
                                     reason="非文字内容尚未解析，需本人查看；不自动回复未知内容", confidence=0)
            draft_id = self.db.create_draft(message.contact, inbound_id, decision)
            self.db.add_event("media_review", decision.reason, contact=message.contact)
            return ProcessResult(status="draft", decision=decision, draft_id=draft_id)
        if (self.config.wechat.ignore_simple_acknowledgments and risk.level == RiskLevel.low
                and re.fullmatch(r"(?:好的?|收到|[Oo][Kk]|嗯+|对|是的|👌|👍)[。.!！,，\s]*", message.content.strip())):
            self.db.add_event("ignored_acknowledgment", "简短确认无需再追加回复，未调用模型", contact=message.contact)
            return ProcessResult(status="ignored")
        recent = self.db.recent_messages(message.contact, self.config.memory.recent_messages)
        memories = self.db.memories(message.contact, self.config.memory.max_contact_memories)
        learned_examples = self.db.style_feedback(message.contact, limit=20)
        from app.greeting_cache import lookup
        cached_greeting=lookup(self.config.resolve(self.config.paths.style_history),message.contact,message.content,self.config.resolve(self.config.paths.role_profile)) if message.message_type=='text' and risk.level==RiskLevel.low else None
        payload = self.prompt_builder.user_payload(
            message, contact, risk, recent, memories, learned_examples
        ) if not cached_greeting else ''

        if cached_greeting:
            decision=ReplyDecision(action='send',risk=RiskLevel.low,reply=cached_greeting,confidence=.95,reason='本机使用已核验的本人问候原话或高频开口习惯；仅回答纯招呼，无事实或新决定，不等待网页模型')
        elif risk.level == RiskLevel.critical:
            # Credentials, bank data, identity documents and legal/compensation details stay local.
            # Do not forward their raw text to an external model.
            decision = ReplyDecision(
                action="hold",
                risk=RiskLevel.critical,
                reply="这件事涉及敏感信息或正式责任，需要本人核对后处理。",
                holding_reply=risk.safe_holding_reply,
                reason="关键敏感事项未发送到外部模型，必须本人确认",
                confidence=1.0,
                facts_to_confirm=["本人核对原始消息和相关凭证"],
            )
        else:
            try:
                if self.llm is None:
                    self.llm = ReplyLLM(self.config)
                decision = self.llm.decide(self.prompt_builder.system_prompt(), payload, risk.level)
            except LLMError as exc:
                self.db.add_event("llm_error", str(exc), level="error", contact=message.contact)
                decision = self._fallback(risk.level, risk.safe_holding_reply, str(exc))

        mode = self._mode(contact)
        if recognized_sticker:
            if not decision.media_description or decision.media_confidence<.75:
                decision.action='review';decision.reason+='；表情识别不确定，留审核'
            else:
                observed_risk=assess_risk(decision.media_description)
                risk.level=self._max_risk(risk.level,observed_risk.level)
                risk.reasons+=observed_risk.reasons
                from app.chat_memory import remember
                remember(self.config.resolve(self.config.paths.chat_memory),[('sticker:'+message.external_id,message.contact,'in',int(message.received_at.timestamp()),'表情包可见内容（非人物事实或承诺）：'+decision.media_description)])
        decision = self._apply_policy(decision, risk.level, mode, risk.safe_holding_reply)
        if (
            decision.risk == RiskLevel.low
            and decision.confidence >= 0.8
            and not decision.facts_to_confirm
        ):
            self.db.add_memories(
                message.contact,
                decision.memory_updates[:3],
                self.config.memory.max_contact_memories,
            )

        if decision.action == "ignore":
            self.db.add_event("ignored", decision.reason or "模型决定不回复", contact=message.contact)
            return ProcessResult(status="ignored", decision=decision)

        if decision.action == "send":
            if self.db.auto_sends_last_hour() >= self.config.wechat.max_auto_sends_per_hour:
                decision.action = "review"
                decision.reason = (decision.reason + "；达到每小时自动发送上限").strip("；")
            else:
                try:
                    ok = adapter.send_text(message.contact, decision.reply)
                except Exception as exc:
                    self.db.add_event("send_error", type(exc).__name__ + ": " + str(exc), level="error", contact=message.contact)
                    ok = False
                if ok:
                    self.db.add_outgoing(message.contact, decision.reply, decision.risk.value)
                    self.db.add_event("auto_sent", decision.reason or "低风险自动回复", contact=message.contact)
                    return ProcessResult(status="sent", decision=decision)
                decision.action = "review"
                skip=getattr(adapter,'last_send_skip_reason','微信发送未确认')
                decision.reason = (decision.reason + "；"+skip).strip("；")
                self.db.add_event('send_guard',skip,contact=message.contact)

        if decision.action == "hold":
            holding = decision.holding_reply or risk.safe_holding_reply
            sent_holding = False
            if holding and self.db.auto_sends_last_hour() < self.config.wechat.max_auto_sends_per_hour:
                try:
                    sent_holding = adapter.send_text(message.contact, holding)
                except Exception as exc:
                    self.db.add_event("send_error", type(exc).__name__ + ": " + str(exc), level="error", contact=message.contact)
                    sent_holding = False
                if sent_holding:
                    self.db.add_outgoing(message.contact, holding, decision.risk.value)
                    self.db.add_event("holding_sent", decision.reason, contact=message.contact)
            draft_id = self.db.create_draft(message.contact, inbound_id, decision)
            return ProcessResult(
                status="holding_and_draft" if sent_holding else "draft",
                decision=decision,
                draft_id=draft_id,
            )

        draft_id = self.db.create_draft(message.contact, inbound_id, decision)
        self.db.add_event("draft_created", decision.reason, contact=message.contact)
        return ProcessResult(status="draft", decision=decision, draft_id=draft_id)

    def send_approved(self, adapter: MessageAdapter) -> int:
        if self.config.mode in {"shadow", "off"}:
            return 0
        sent = 0
        for draft in self.db.approved_drafts():
            text = (draft.edited_reply or draft.reply).strip()
            if not text:
                self.db.update_draft(draft.id, "failed")
                continue
            # Claim before touching the UI so a crash cannot silently resend the same approval.
            self.db.update_draft(draft.id, "sending")
            try:
                if draft.kind == 'proactive':
                    if not hasattr(adapter,'prepare_proactive'):
                        raise RuntimeError('当前连接不支持经过核验的主动联系')
                    adapter.prepare_proactive(draft.contact,draft.id,draft.source_context_ts)
                adapter.manual_approval_in_progress = True
                ok = adapter.send_text(draft.contact, text)
            except Exception as exc:
                self.db.add_event("approved_send_error", str(exc), level="error", contact=draft.contact)
                ok = False
            finally:
                adapter.manual_approval_in_progress = False
            if ok:
                self.db.add_outgoing(draft.contact, text, draft.risk)
                self.db.update_draft(draft.id, "sent")
                self.db.add_event("approved_sent", f"草稿 #{draft.id} 已发送", contact=draft.contact)
                sent += 1
            else:
                self.db.update_draft(draft.id, "failed")
        return sent
