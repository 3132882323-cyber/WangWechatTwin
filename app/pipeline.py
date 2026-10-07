from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import re
import json
from datetime import datetime,timezone,timedelta

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
        from threading import Lock
        self.llm_init_lock=Lock()
        self.contact_map = config.contact_map()

    def _sticker_suggestion(self,message,inbound_id):
        if message.message_type!='text' or assess_risk(message.content).level!=RiskLevel.low:return
        from app.sticker_catalog import suggest
        entry=suggest(self.config,message.content)
        if not entry:return
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        state=self.db.get_state('sticker_suggestions') or {};key=message.contact+'|'+today
        if state.get(key,0)>=self.config.stickers.max_per_contact_per_day:return
        with self.db.connect() as connection:
            if connection.execute("SELECT 1 FROM drafts WHERE contact=? AND kind='sticker' AND status IN ('pending','approved')",(message.contact,)).fetchone():return
        self.db.create_draft(message.contact,inbound_id,ReplyDecision(action='review',risk=RiskLevel.low,
            reply='[表情包图片：'+entry['label']+']',sticker_id=entry['id'],confidence=.85,
            reason='对方明确分享好消息，建议用已批准表情库中的图片庆祝；批准后才发送'),kind='sticker')
        state[key]=state.get(key,0)+1;self.db.set_state('sticker_suggestions',state)
        self.db.add_event('sticker_draft','表情图片建议待审核',contact=message.contact)

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

    @staticmethod
    def _neutral_date_question(message, decision, risk, route) -> bool:
        if (
            message.message_type != "text" or route != "deepseek"
            or risk.level != RiskLevel.medium
            or risk.reasons != ["包含日期、数量、金额或交付信息"]
            or decision.action != "send" or decision.risk != RiskLevel.low
            or decision.confidence < .9 or decision.facts_to_confirm
        ):
            return False
        try:
            origin = json.loads(message.raw_summary or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(origin, dict) or any(origin.get(key) for key in (
            "asr_uncertain", "resume_incomplete", "owner_corrected_input",
        )):
            return False
        # This exception only covers a stated calendar day, never an amount,
        # quantity, or a request for the owner's action.
        without_dates, date_count = re.subn(r"(?<!\d)(?:[1-9]|[12]\d|3[01])\s*(?:号|日)", "", message.content)
        if not date_count or re.search(r"\d|[零〇一二三四五六七八九十百千万两]|钱|元|块|费用|资金|帮|替|代办|陪|一起|咱|我们|能来|能否|能不能|可以|要不要|麻烦|请|安排|约|交付|完工|开工|完成|做好|送货|送到|出发|到达|见面|订票|订房|截止|到期|承诺|保证|你.*(?:来|搬|做)", without_dates):
            return False
        reply = decision.reply.strip()
        if not 0 < len(reply) <= 40 or re.search(
            r"\d|[零〇一二三四五六七八九十百千万两]|我|咱|一起|承诺|保证|答应|负责|帮|替|陪|能否|能不能|可以|要不要|请|麻烦",
            reply,
        ) or assess_risk(reply).level != RiskLevel.low:
            return False
        # Full-match a single question about the other person's current state.
        # Statements, agreements, action requests, and additional clauses fail.
        return bool(re.fullmatch(
            r"(?:(?:(?:你(?:那边)?的?|那边的?)?)(?:东西|行李|物品|房间|新家|住处)|你(?:那边|现在|最近)?)"
            r"(?:都|已经|现在)?(?:收拾|整理|打包|准备|安顿|搬)"
            r"(?:(?:得差不多|好|完|齐|妥当)?了?(?:吗|没|没有)|得怎么样了?)[？?]?",
            reply,
        ))

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

    def process(self,message:IncomingMessage,adapter:MessageAdapter)->ProcessResult:
        import time
        origin=json.loads(message.raw_summary or '{}')
        recovering=bool(origin.get('resume_incomplete'))
        with self.db.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS reply_outcomes(external_id TEXT PRIMARY KEY,status TEXT,updated REAL)')
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute('SELECT status FROM reply_outcomes WHERE external_id=?',(message.external_id,)).fetchone()
            if previous and (previous[0]!='processing' or not recovering):
                conn.execute('COMMIT');return ProcessResult(status='duplicate')
            if recovering and self.db.seen(message.external_id):
                handled=conn.execute('SELECT 1 FROM drafts d JOIN messages m ON m.id=d.inbound_message_id WHERE m.external_id=?',(message.external_id,)).fetchone()
                if handled:
                    conn.execute('COMMIT');return ProcessResult(status='duplicate')
            conn.execute("INSERT OR REPLACE INTO reply_outcomes VALUES(?,'processing',?)",(message.external_id,time.time()))
            conn.execute('COMMIT')
        result=self._process(message,adapter)
        with self.db.connect() as conn:conn.execute('UPDATE reply_outcomes SET status=?,updated=? WHERE external_id=?',(result.status,time.time(),message.external_id))
        return result

    def _process(self, message: IncomingMessage, adapter: MessageAdapter) -> ProcessResult:
        if self.db.seen(message.external_id) and not json.loads(message.raw_summary or '{}').get('resume_incomplete'):
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
        media_route=ReplyLLM.route(json.dumps({'incoming':{'content':message.content,'message_type':message.message_type},'__media_paths':message.media_paths}),risk.level)
        if media_route=='doubao' and self.config.openai.provider in {'hybrid_web','doubao_web'}:vision_ready=bool((self.db.get_state('doubao_bridge') or {}).get('ready'))
        recognized_sticker=message.message_type=='sticker' and 0<len(message.media_paths)<=3 and self.config.openai.provider in {'web','hybrid_web','doubao_web'} and vision_ready
        recognized_image=message.message_type=='image' and 0<len(message.media_paths)<=3 and self.config.openai.provider in {'web','hybrid_web','doubao_web'} and vision_ready
        if message.message_type != "text" and not (recognized_sticker or recognized_image):
            decision = ReplyDecision(action="review", risk=RiskLevel.medium, reply="",
                                     reason="非文字内容尚未解析，需本人查看；不自动回复未知内容", confidence=0)
            draft_id = self.db.create_draft(message.contact, inbound_id, decision)
            self.db.add_event("media_review", decision.reason, contact=message.contact)
            return ProcessResult(status="draft", decision=decision, draft_id=draft_id)
        from app.reply_quality import laughter_only,trim_laughter
        if message.message_type=='text' and laughter_only(message.content) and risk.level==RiskLevel.low:
            self.db.add_event('ignored_laughter','对方只有笑声，话题自然收尾，不再追加笑声',contact=message.contact)
            return ProcessResult(status='ignored')
        if (self.config.wechat.ignore_simple_acknowledgments and risk.level == RiskLevel.low
                and re.fullmatch(r"(?:好的?|收到|[Oo][Kk]|嗯+|对|是的|👌|👍)[。.!！,，\s]*", message.content.strip())):
            self.db.add_event("ignored_acknowledgment", "简短确认无需再追加回复，未调用模型", contact=message.contact)
            return ProcessResult(status="ignored")
        recent = self.db.recent_messages(message.contact, self.config.memory.recent_messages)
        memories = self.db.memories(message.contact, self.config.memory.max_contact_memories)
        learned_examples = self.db.style_feedback(message.contact, limit=20)
        from app.greeting_cache import lookup
        cached_greeting=lookup(self.config.resolve(self.config.paths.style_history),message.contact,message.content,self.config.resolve(self.config.paths.role_profile)) if self.config.wechat.use_greeting_cache and message.message_type=='text' and risk.level==RiskLevel.low else None
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
                with self.llm_init_lock:
                    if self.llm is None:self.llm = ReplyLLM(self.config)
                decision = self.llm.decide(self.prompt_builder.system_prompt(), payload, risk.level)
            except LLMError as exc:
                self.db.add_event("llm_error", str(exc), level="error", contact=message.contact)
                decision = self._fallback(risk.level, risk.safe_holding_reply, str(exc))

        if decision.action!='ignore' and (laughter_only(decision.reply) or not decision.reply.strip()):
            try:
                corrected=json.loads(payload)
                corrected['reply_quality_feedback']={'rejected_reply':decision.reply,
                    'instruction':'这个回复没有有效正文或只有笑声，没有回应对方内容。请重新理解本轮消息与上下文，认真给出有内容的本人式回复；不需要接话则 ignore。review 也必须包含草稿正文。不要编造事实或承诺。'}
                self.db.add_event('reply_quality_retry','空泛笑声回复已拦截，重新生成一次',contact=message.contact)
                decision=self.llm.decide(self.prompt_builder.system_prompt(),json.dumps(corrected,ensure_ascii=False),risk.level)
            except (LLMError,ValueError,AttributeError):
                decision.action='review';decision.reply='';decision.reason+='；空泛笑声回复已拦截，需要本人审核'
            if decision.action!='ignore' and (laughter_only(decision.reply) or not decision.reply.strip()):
                decision.action='review';decision.reply='';decision.confidence=0
                decision.facts_to_confirm.append('需要一条真正回应当前内容的回复')
                decision.reason+='；重新生成仍没有实质内容，不自动发送'
                draft_id=self.db.create_draft(message.contact,inbound_id,decision)
                return ProcessResult(status='draft',decision=decision,draft_id=draft_id)
        if not laughter_only(decision.reply):decision.reply=trim_laughter(decision.reply)
        mode = self._mode(contact)
        if any(json.loads(message.raw_summary or '{}').get(key) for key in ('owner_corrected_input','resume_incomplete')):
            mode='shadow';decision.reason+='；中断恢复或本人纠正，只起草，不重复发送'
        if media_route=='chatgpt' and re.search('气死|气炸|气疯|愤怒|崩溃|烦死|激动|受不了了|忍不住哭|别烦我|你是不是有病|骗子|混蛋|滚开|分手|不想活|吵架|绝交|别再联系',message.content):
            mode='shadow';decision.reason+='；明显激动情绪，先供本人审核'
        if recognized_sticker or recognized_image:
            if not decision.media_description or decision.media_confidence<.75:
                decision.action='review';decision.reason+='；图片或表情识别不确定，留审核'
                decision.facts_to_confirm.append('图片或表情的可见内容尚未可靠识别')
            else:
                observed_risk=assess_risk(decision.media_description)
                risk.level=self._max_risk(risk.level,observed_risk.level)
                risk.reasons+=observed_risk.reasons
                from app.chat_memory import remember
                caption_prefix='图片可见内容（非当前事实或承诺）：' if recognized_image else '表情包可见内容（非人物事实或承诺）：'
                remember(self.config.resolve(self.config.paths.chat_memory),[('media:'+message.external_id,message.contact,'in',int(message.received_at.timestamp()),caption_prefix+decision.media_description)])
            if recognized_image:
                decision.action='review';decision.reason+='；普通图片回复先审核，不自动确认图片中的事实'
                decision.facts_to_confirm.append('本人确认普通图片的可见内容和拟回复')
        try:
            media_origin=json.loads(message.raw_summary or '{}')
            if media_origin.get('asr_uncertain'):
                decision.action='review';decision.facts_to_confirm.append('语音听写含模糊内容或重要数字，需要核对原语音')
                decision.reason+='；语音转写需要核对'
        except ValueError:pass
        neutral_date_question = mode == "low_risk_auto" and self._neutral_date_question(
            message, decision, risk, media_route,
        )
        if neutral_date_question:
            decision.reason = (decision.reason + "；输入日期仍记为中风险，仅发送无数字、无承诺的对方状态问句").strip("；")
        policy_risk = RiskLevel.low if neutral_date_question else risk.level
        decision = self._apply_policy(decision, policy_risk, mode, risk.safe_holding_reply)
        if (
            decision.risk == RiskLevel.low
            and decision.confidence >= 0.8
            and not decision.facts_to_confirm
            and not (recognized_image or recognized_sticker)
            and not neutral_date_question
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
                    self._sticker_suggestion(message,inbound_id)
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

    @staticmethod
    @contextmanager
    def _approval_scope(adapter,contact,draft_id):
        if hasattr(adapter,'bind_approval'):
            with adapter.bind_approval(contact,draft_id):yield
            return
        # Older adapters still expect these fields; HTTP approval is context-local.
        adapter.manual_approval_in_progress=True
        adapter.approved_draft_id=draft_id
        try:yield
        finally:adapter.manual_approval_in_progress=False

    def send_approved(self, adapter: MessageAdapter) -> int:
        if self.config.mode in {"shadow", "off"}:
            return 0
        sent = 0
        for draft in self.db.approved_drafts():
            if draft.kind=='deepseek_candidate':
                self.db.update_draft(draft.id,'pending');continue
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
                with self._approval_scope(adapter,draft.contact,draft.id):
                    ok = adapter.send_sticker(draft.contact,draft.sticker_id) if draft.kind=='sticker' else adapter.send_text(draft.contact, text)
            except Exception as exc:
                self.db.add_event("approved_send_error", str(exc), level="error", contact=draft.contact)
                ok = False
            if ok:
                self.db.add_outgoing(draft.contact, text, draft.risk)
                self.db.update_draft(draft.id, "sent")
                self.db.add_event("approved_sent", f"草稿 #{draft.id} 已发送", contact=draft.contact)
                sent += 1
            else:
                self.db.update_draft(draft.id, "failed")
        return sent
