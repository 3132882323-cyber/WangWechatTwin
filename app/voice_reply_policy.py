"""Pure local advice for the review desk; never generates or sends audio.

``text`` is the conservative default. ``voice_review`` means one short draft
may be worth previewing, then approving for that recipient and exact content.
Neither result grants permission to send or changes an existing reply decision.
The duration is a rough reading estimate, not measured synthesis latency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Literal, Sequence


MAX_VOICE_SECONDS = 15.0


@dataclass(frozen=True)
class OwnerHumanVoiceStats:
    """Caller-verified, recent manual-owner samples for this contact only.

    A WeChat outgoing record by itself does not prove human authorship. The
    caller must exclude generated, unknown and media-observation records before
    asserting ``verified_human_only``. Missing evidence stays unknown.
    """

    sample_count: int = 0
    voice_count: int = 0
    source: str = "unknown"
    same_contact: bool = False
    verified_human_only: bool = False
    recent_window_days: int = 30
    last_voice_age_days: float | None = None


@dataclass(frozen=True)
class VoiceReplySuggestion:
    medium: Literal["text", "voice_review"]
    reason: str
    estimated_voice_seconds: float | None
    # Approval for the proposed voice medium only. A text result never clears
    # the reply's existing risk, fact-confirmation or manual-review boundary.
    requires_approval: bool

    def as_dict(self) -> dict:
        return asdict(self)


_QUOTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|“[^”]*(?:”|$)|‘[^’]*(?:’|$)|'
    r'"[^"\n]*(?:"|$)|(?<!\w)\'[^\'\n]*\''
)
_REPORTED = re.compile(
    r"转发|引用|转达|聊天记录|"
    r"(?:朋友|同事|客户|别人|对方|他|她|他们|她们|老板)(?:刚才|之前|刚|曾经)?(?:说|要求|让|叫|告诉|问|发来|发给我)|"
    r"有人说|(?:he|she|they)\s+(?:said|asked)", re.I
)
_REPORTED_MULTILINE = re.compile(
    r"(?:转发|引用|转达|聊天记录)[^\n]*\n|(?:他|她)(?:说|让|叫)[^\n]*[：:]\s*\n|"
    r"(?:别人|他人|朋友|同事|客户|对方|他|她).{0,10}(?:发来|发给我|原话|说的|问的)[^\n]*\n"
)
_FUTURE_OR_CONDITIONAL = re.compile(
    r"以后|下次|一会儿?|待会儿?|等会儿?|回头|明天|改天|有空|如果|假如|要是|"
    r"等我|等你|先别|暂时|稍后|later|next time|tomorrow|\bif\b", re.I
)
_VOICE_REQUEST = re.compile(
    r"(?:发|来|录|回)(?:一条|一段|一个|一下|一|个|条|段){0,2}语音|"
    r"(?:用|通过|以)(?:一条)?语音(?:给我)?(?:回复|回我|说|讲|告诉)|"
    r"语音(?:回复|回我|说|讲|告诉我)|"
    r"(?:我想听听|我想听|想听听|让我听)(?:你(?:的)?)?(?:声音|语音)|"
    r"(?:please\s+)?send\s+(?:me\s+)?(?:a\s+)?(?:voice|audio)\s+message|"
    r"reply\s+(?:with|by|in)\s+(?:a\s+)?(?:voice|audio)", re.I
)
_REQUEST_PREFIX = re.compile(
    r"(?:(?:请|麻烦|你|您|那|要不|不如|还是|直接|给我|现在|今天|方便|可以|能|能否|可否|"
    r"我希望你|我想你|希望你|我想让你|我想请你|can you|could you|would you)\s*)*", re.I
)
_REQUEST_SUFFIX = re.compile(
    r"(?:(?:给我|回我|回复我|我)?(?:听)?(?:一下)?(?:吧|吗|嘛|呀|啊|呗|哦|好吗|行吗|可以吗|就好|就行)?|"
    r"(?:给我)?(?:说说|说一下|讲一下|讲讲|解释一下|告诉我|让我听听|让我听|听听|聊聊)(?![的了过得])[^\n]{0,80}|"
    r"(?:一下|一说)(?![的了过得])[^\n]{0,80})"
)
_REQUEST_DISCUSSION = re.compile(r"难度|速度|方法|好处|坏处|还是文字|还是打字|的人|的时候|你为什么|你怎么")
_VOICE_NEGATED = re.compile(
    r"(?:别|不要|不用|不必|不能|不可以|不想|没(?:有)?(?:让|叫|要)|不是|不发|无需|禁止)"
    r".{0,12}(?:语音|音频|声音)|\b(?:don't|do not|no|not)\b.{0,18}(?:voice|audio)", re.I
)
_TEXT_REQUEST = re.compile(
    r"(?:别|不要|不用|不必|不想|不爱|不喜欢|无需|不需要).{0,8}(?:语音|音频)|"
    r"(?:语音|音频).{0,5}(?:不方便听|听不了|没法听|别发|不要发)|"
    r"不方便听|听不了|没法听|没空听|不能听|不想听|"
    r"(?:文字|打字)(?:回复|回我|说|告诉我|就好|就行|吧|给我|可以吗|好吗)|"
    r"(?:用|发|回|给我发|直接|请)(?:文字|打字)(?:$|[啊呀呢])|"
    r"我想看文字|只能看文字|只能打字|文字就(?:可以|好|够)|手机静音|(?:正在|在)开会|(?:正在|在)上课|"
    r"\b(?:prefer text|text me|reply in text|can't listen|cannot listen)\b", re.I
)
_NEGATED_TEXT = re.compile(r"(?:别|不要|不用|不必|无需)(?:再)?(?:用|发)?(?:文字|打字|写字)")
_FILE_REQUEST = re.compile(r"(?:音频|语音|录音).{0,5}(?:附件|文件)|(?:附件|文件).{0,5}(?:音频|语音|录音)|\.(?:mp3|wav|ogg|flac)\b", re.I)
_SENDER_VOICE_INTENT = re.compile(
    r"我(?:想|要|准备|打算|来|可以|会|先|再|现在|刚才|刚|已经|正在|给你|给您|还是)*"
    r"(?:发|录|回)(?:一条|一段|一个|一下|一|个|条|段){0,2}语音"
)
_EXACT_INFORMATION = re.compile(
    r"\d|https?://|www\.|[a-z0-9-]+\.(?:com|cn|net|org)(?:/|\b)|`|"
    r"[一二三四五六七八九十百千万两]+(?:元|块|万元|点|时|天|号|日|月|年|米|厘米|公斤|吨|套)|"
    r"星期[一二三四五六日天]|(?:本|下|这)?周[一二三四五六日天]|"
    r"(?:今天|明天|后天|本周|下周|月底).{0,8}(?:见|来|到|送|约|安排|截止|开工|交付)|"
    r"(?:地址|门牌|邮编|定位|账号|账户|号码|金额|报价|价格|尺寸|日期|时间表|链接)|"
    r"(?:路|街|大道|小区|大厦|楼栋|单元).{0,5}(?:号|栋|室|楼)|"
    r"\b(?:SELECT|INSERT|UPDATE|import|def|curl)\b", re.I
)
_PROFESSIONAL = re.compile(
    r"工作|项目|业务|合同|协议|发票|税点|工期|交期|承诺|保证|转账|汇款|退款|赔偿|"
    r"法律|律师|诊断|处方|剂量|病情|图纸|施工|技术方案|专业问题|"
    r"代码|函数|接口|配置|报错|命令|验证码|密码|API", re.I
)
_ACTIVATED_EMOTION = re.compile(r"吵架|争吵|气死|生气|愤怒|投诉|你骗我|别骗我|滚开|滚蛋|受不了了")
_CLOSING = re.compile(r"晚安|先睡|去睡|休息了|先忙|忙去了|先这样|不聊了|不想聊|改天聊|回头聊|明天聊|拜拜|再见")
_SHORT_ACK = re.compile(r"好{1,3}|好的|好嘞|行|可以|收到|知道了|明白了|谢谢(?:你)?|嗯{1,3}|哦{1,3}|没问题|ok|哈{2,}", re.I)


def estimate_voice_seconds(text: str) -> float | None:
    """Estimate reading duration with a fixed rate; no model or audio is used."""
    if not isinstance(text, str) or not text.strip():
        return None
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text))
    digits = len(re.findall(r"\d", text))
    pauses = len(re.findall(r"[,，、;；]", text)) * 0.15 + len(re.findall(r"[。.!！?？\n]", text)) * 0.25
    seconds = chinese / 4.0 + words / 2.5 + digits / 3.0 + pauses
    return math.ceil(max(1.0, seconds) * 10) / 10


def _direct_clauses(text: str) -> list[str]:
    # Quoted or reported words describe someone else's request, not this peer's
    # current instruction. Treat uncertain forwarding conservatively.
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    unquoted = _QUOTED.sub(" ", "\n".join(lines))
    if _REPORTED_MULTILINE.search(unquoted):
        return []
    return [clause.strip() for clause in re.split(r"[，,。.!！?？;；\n]+", unquoted)
            if clause.strip() and not _REPORTED.search(clause)]


def _has_text_request(clauses: list[str]) -> bool:
    for clause in clauses:
        polite = re.sub(r"能不能|可不可以|会不会|要不要", "可以", _NEGATED_TEXT.sub("", clause))
        if _TEXT_REQUEST.search(polite) or _VOICE_NEGATED.search(polite) or re.fullmatch(r"文字|打字", clause):
            return True
    return False


def _has_current_voice_request(clauses: list[str]) -> bool:
    for clause in clauses:
        polite = re.sub(r"能不能|可不可以|会不会|要不要", "可以", _NEGATED_TEXT.sub("", clause))
        request = _VOICE_REQUEST.search(polite)
        if (request and _REQUEST_PREFIX.fullmatch(polite[:request.start()].strip())
                and _REQUEST_SUFFIX.fullmatch(polite[request.end():].strip())
                and not _REQUEST_DISCUSSION.search(polite) and not _VOICE_NEGATED.search(polite)
                and not _FUTURE_OR_CONDITIONAL.search(polite) and not _SENDER_VOICE_INTENT.search(polite)):
            return True
    return False


def _has_verified_habit(stats: OwnerHumanVoiceStats | None) -> bool:
    if not isinstance(stats, OwnerHumanVoiceStats):
        return False
    if (stats.source not in {"wechat_original", "owner_confirmed_wechat_history"} or stats.same_contact is not True
            or stats.verified_human_only is not True):
        return False
    if any(type(value) is not int for value in (stats.sample_count, stats.voice_count, stats.recent_window_days)):
        return False
    age = stats.last_voice_age_days
    return bool(
        stats.sample_count >= 5 and 3 <= stats.voice_count <= stats.sample_count
        and stats.voice_count / stats.sample_count >= 0.25
        and 1 <= stats.recent_window_days <= 30
        and type(age) in (int, float) and math.isfinite(age)
        and 0 <= age <= min(14, stats.recent_window_days)
    )


def suggest_reply_medium(
    *, incoming_text: str = "", incoming_type: str = "text", chat_type: str = "friend",
    reply_text: str = "", risk: str = "low", confidence: float = 1.0,
    facts_to_confirm: Sequence[str] = (), review_required: bool = False,
    asr_uncertain: bool = False, professional: bool = False,
    emotionally_activated: bool = False, local_hour: int | None = None,
    recipient_preference: str = "unknown", familiar_contact: bool = False,
    owner_voice_stats: OwnerHumanVoiceStats | None = None,
) -> VoiceReplySuggestion:
    """Return one recommendation without changing reply text or send policy.

    ``incoming_type`` should use the original media type, before ASR replaces it
    with text. ``familiar_contact`` requires evidence of existing exchanges; it
    does not imply personal closeness. Preferences require evidence from the caller;
    an inbound voice, outgoing type34 row or generated preview cannot supply it.
    Explicit current voice requests can be reviewed without assuming familiarity.
    """
    duration = estimate_voice_seconds(reply_text)

    def text(reason: str) -> VoiceReplySuggestion:
        return VoiceReplySuggestion("text", reason, duration, False)

    if duration is None:
        return text("还没有完整回复草稿，先保留文字建议。")
    incoming = incoming_text if isinstance(incoming_text, str) else ""
    clauses = _direct_clauses(incoming)
    if _has_text_request(clauses) or recipient_preference in {"text", "text_only"}:
        return text("对方希望看文字或不方便听，优先文字。")
    if chat_type not in {"friend", "private", "direct"}:
        return text("群聊或会话类型不明确，优先文字。")
    if (review_required or facts_to_confirm or asr_uncertain
            or getattr(risk, "value", risk) != "low"
            or type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0.8 <= confidence <= 1.0):
        return text("内容、风险或语音转写仍需核对，保留文字并沿用原审核要求。")
    combined = incoming + "\n" + reply_text
    if _EXACT_INFORMATION.search(combined):
        return text("包含数字、日期、地址、代码或链接等准确信息，文字更便于核对。")
    if professional or _PROFESSIONAL.search(combined):
        return text("涉及工作、专业问题或承诺，文字更便于核对。")
    if emotionally_activated or _ACTIVATED_EMOTION.search(combined):
        return text("对话有争执或明显激动情绪，先用文字核对并保留审核。")
    if local_hour is None or type(local_hour) is not int or not 0 <= local_hour <= 23:
        return text("本机时段信息不明确，优先文字。")
    if local_hour >= 22 or local_hour < 8:
        return text("当前处于夜间或清早安静时段，优先文字。")
    if any(_FUTURE_OR_CONDITIONAL.search(clause) for clause in clauses):
        return text("对方提到以后或满足条件再说，当前先保留文字。")
    if _FILE_REQUEST.search(combined):
        return text("提到音频文件或附件，不能当作微信语音回复请求，先保留文字。")
    if duration > MAX_VOICE_SECONDS:
        return text(f"这条回复估计需读约{duration:g}秒，超过短语音目标，先保留文字。")
    explicit_request = _has_current_voice_request(clauses)
    if explicit_request:
        return VoiceReplySuggestion(
            "voice_review", f"对方本轮明确希望听语音；估计约{duration:g}秒，需先试听并逐条批准。",
            duration, True,
        )
    plain_reply = re.sub(r"[\s，,。.!！?？~～…]+", "", reply_text)
    if _SHORT_ACK.fullmatch(plain_reply) or len(plain_reply) <= 4:
        return text("只是简短确认，文字就够了。")
    if _CLOSING.search(combined):
        return text("对方或回复正在收尾、休息或忙碌，保留简短文字。")
    if familiar_contact is not True:
        return text("尚无既有交流的可靠依据，默认文字。")
    if incoming_type != "voice":
        return text("本轮没有明确语音请求，对方也未用语音，默认文字。")
    if not _has_verified_habit(owner_voice_stats):
        return text("对方用了语音，但同联系人近期本人手发语音的可信样本不足，默认文字。")
    return VoiceReplySuggestion(
        "voice_review", f"此前给该联系人发过语音，且有近期本人语音习惯；本轮可考虑一条短语音，估计约{duration:g}秒，需试听并批准。",
        duration, True,
    )
