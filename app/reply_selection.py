"""Select one reply from a single model response; never make another model call."""
from __future__ import annotations

import json
import re
from typing import Any

from app.models import RISK_ORDER, ReplyDecision, RiskLevel
from app.reply_quality import laughter_only
from app.risk import assess_risk


# Routing to GPT for emotion or risk does not itself make a message a knowledge
# question. Keep this classifier independent of that routing decision.
_PROFESSIONAL = re.compile(
    r"专业(?:知识|问题)|技术(?:问题|原理|方案)|解释清楚|合同|报价|施工|工期|工程|安装|配置|"
    r"材料|板材|保温|门窗|踢脚线|瓷砖|防水|钢筋|混凝土|配电|光合作用|科普|排期|调度|财务|电路|代码|编程|程序|算法|数据库|"
    r"法律|税务|税点|发票|保险|投资|诊断|治疗|药物|用药|预算|尺寸|图纸|"
    r"物理|化学|数学|量子|定理|公式|医学|经济学|机械|电气|结构力学|"
    r"方案|(?:解释|讲解|说明).{0,12}(?:原理|机制)|"
    r"(?<![a-z0-9_])(?:python|javascript|typescript|sql|api|html|css|c\+\+|java|linux)(?![a-z0-9_])",
    re.IGNORECASE,
)
_EMOTIONAL = re.compile(
    r"气死|气炸|气疯|愤怒|崩溃|烦死|激动|受不了了|忍不住哭|别烦我|"
    r"你是不是有病|骗子|混蛋|滚开|分手|不想活|吵架|绝交|别再联系"
)
_ENDING = re.compile(
    r"晚安|先忙|忙去了|去忙了|去睡|睡了|睡觉了|要休息了|想休息|休息了|"
    r"不聊了|回头聊|不想说|别问了|不用回复|先这样|别再联系|别烦我"
)
_QUESTION = re.compile(r"[?？]|(?:吗|呢)[。！!\s]*$|咋|怎么|为什么|啥|哪儿|哪里|几点|多少")
_FALSE_HUMAN_CLAIM = re.compile(
    r"(?:我(?:可)?不是|我(?:真)?不是什么|不是)\s*(?:AI|人工智能|机器人|智能助理)|"
    r"我(?:就是|是|真是|当然是|可是)\s*(?:真人|本人|活人)|"
    r"(?:真人|本人)(?:在回|回复|发的)",
    re.IGNORECASE,
)
_SCORES = ("style_match", "context_fit", "continuation", "boundary_respect")
_REVIEW_MARKER = "A/B/C 候选未通过本机选择检查，须本人审核"


def payload_data(user_payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(user_payload, dict):
        return dict(user_payload)
    try:
        parsed = json.loads(user_payload)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # Older API callers supplied plain text. Preserve it as message data.
    return {"incoming": {"content": user_payload if isinstance(user_payload, str) else ""}}


def incoming_text(user_payload: str | dict[str, Any]) -> str:
    incoming = payload_data(user_payload).get("incoming", {})
    return str(incoming.get("content", "")) if isinstance(incoming, dict) else ""


def is_professional_request(user_payload: str | dict[str, Any]) -> bool:
    return bool(_PROFESSIONAL.search(incoming_text(user_payload)))


def is_emotional_request(user_payload: str | dict[str, Any]) -> bool:
    return bool(_EMOTIONAL.search(incoming_text(user_payload)))


def prepare_selection_request(system_prompt: str, user_payload: str) -> tuple[str, str, str]:
    """Put mode and rubric into the same request as the existing context."""
    payload = payload_data(user_payload)
    mode = "direct" if is_professional_request(payload) else "abc"
    if mode == "direct":
        policy = (
            "本轮是专业知识或业务问题：根据证据直接给出准确、清楚的回复，不做 A/B/C 候选比较。"
            "candidates 与 selected_candidate 留空（null）或省略；reply 保留直接答案。"
            "原有事实核对、风险审核和身份诚实规则继续适用。"
        )
        payload["reply_selection"] = {"mode": mode, "candidate_count": 0}
    else:
        policy = (
            "本轮用一次输出完成日常回复的内部 A/B/C 起草和评估，不另开请求。"
            "先理解对方这句话、前文和本人最新反馈，在同一个 JSON 的 candidates 中写恰好三个"
            "不同且非空的可用短回复，label 分别为 A、B、C，每条最多 600 字；不要把 A/B/C 标签"
            "或评语写进对外 reply。三条都要先具体接住对方的内容，用当前联系人场景下本人的"
            "自然表达，可以有不同措辞或轻重，但不能靠编造本人经历、喜好、动机或承诺来拉近关系。"
            "每条给 style_match、context_fit、continuation、boundary_respect 四个 0～5 主观评分，"
            "以及 invented_facts 布尔值和 risk（low/medium/high/critical）。评分只是本轮排序意见，"
            "不是正确率、概率或事实证明。优先级为：没有编造且安全、尊重边界、回应当前内容、"
            "贴近本人表达，最后才是让人愿意继续聊。只在自然时留一个低负担接话点，最多一个问题；"
            "不重复已答问题，对晚安、休息、先忙、拒绝和不想说自然收尾，不为续聊拉住对方。"
            "selected_candidate 可填建议标签，reply 写建议正文，本机仍会按固定规则选择。"
            "action=ignore 表示确实无需回复，此时不要求 A/B/C，候选与选择标签留空。"
            "media_description、media_confidence、sticker_id、facts_to_confirm 等原有字段照常提供。"
            "三条候选和模型评分都是 AI 草稿，不能作为新的本人事实、本人原话或训练样例。"
            "若被问是不是 AI，应如实说明是本人设置的智能助理，不得声称真人或否认 AI 身份。"
        )
        payload["reply_selection"] = {
            "mode": mode,
            "candidate_count": 3,
            "labels": ["A", "B", "C"],
            "score_range": [0, 5],
            "score_meaning": "本轮主观排序意见，不是概率或事实证明",
            "ranking_priority": [
                "no_invented_facts", "risk", "boundary_respect", "context_fit", "style_match", "continuation"
            ],
        }
    # The web backend trims long historical sections from the end of the system
    # prompt. Prepending retains this trusted instruction on compact providers.
    return policy + "\n\n" + system_prompt, json.dumps(payload, ensure_ascii=False), mode


def _max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=RISK_ORDER.__getitem__)


def _append_reason(decision: ReplyDecision, text: str) -> None:
    decision.reason = (decision.reason + "；" + text).strip("；")


def _require_review(decision: ReplyDecision, reason: str) -> ReplyDecision:
    decision.action = "review"
    decision.memory_updates = []
    decision.confidence = min(decision.confidence, 0.5)
    if _REVIEW_MARKER not in decision.facts_to_confirm:
        decision.facts_to_confirm.append(_REVIEW_MARKER)
    _append_reason(decision, reason)
    return decision


def _has_closing_cue(message: str) -> bool:
    for match in _ENDING.finditer(message):
        prefix = message[max(0, match.start() - 8):match.start()]
        # "不想休息" and "还没去睡" are not affirmative closing cues.
        if re.search(r"(?:不|没|没有|别|不用|不是)\s*$", prefix):
            continue
        if match.group() == "晚安" and re.match(r"(?:表情|的意思|是什么意思|怎么说|应该怎么)", message[match.end():]):
            continue
        return True
    return False


def _candidate_issues(candidate: Any, message: str) -> list[str]:
    issues = []
    if candidate.invented_facts:
        issues.append("候选标记了未经确认的本人事实")
    if _FALSE_HUMAN_CLAIM.search(candidate.reply):
        issues.append("候选包含不实的真人身份表述")
    if laughter_only(candidate.reply):
        issues.append("候选只含笑声或笑脸")
    if len(message.strip()) >= 6 and not _has_closing_cue(message) and re.fullmatch(
        r"(?:嗯+|哦+|好|好的|好啊|行|可以|收到|知道了|是的|ok)[\s。.!！,，]*", candidate.reply.strip(), re.I
    ):
        issues.append("对方有具体内容，候选却只作空泛确认")
    if len(re.findall(r"[?？]+", candidate.reply)) > 1:
        issues.append("候选包含多个跟进问题")
    if _has_closing_cue(message) and _QUESTION.search(candidate.reply):
        issues.append("对方已收尾，候选仍在追问")
    if candidate.boundary_respect < 3:
        issues.append("候选未达到边界要求")
    return issues


def _valid_candidates(candidates: Any) -> bool:
    if not isinstance(candidates, list) or len(candidates) != 3:
        return False
    try:
        if {candidate.label for candidate in candidates} != {"A", "B", "C"}:
            return False
        # Normalized duplicates do not provide three meaningful drafts.
        texts = [re.sub(r"[\s，,。.!！?？…~～]+", "", c.reply).casefold() for c in candidates]
        if not all(texts) or len(set(texts)) != 3:
            return False
        for candidate in candidates:
            if len(candidate.reply) > 600 or not candidate.reply.strip():
                return False
            if candidate.risk not in RISK_ORDER or not isinstance(candidate.invented_facts, bool):
                return False
            if any(isinstance(getattr(candidate, field), bool)
                   or not 0 <= getattr(candidate, field) <= 5 for field in _SCORES):
                return False
    except (AttributeError, TypeError):
        return False
    return True


def select_reply(
    decision: ReplyDecision,
    *,
    mode: str,
    user_payload: str | dict[str, Any],
    incoming_risk: RiskLevel,
) -> ReplyDecision:
    """Deterministic selection preserves all existing non-selection metadata."""
    result = decision.model_copy(deep=True)
    if result.action == "ignore":
        result.candidates = None
        result.selected_candidate = None
        return result
    if mode == "direct":
        result.candidates = None
        result.selected_candidate = None
        result.risk = _max_risk(result.risk, incoming_risk, assess_risk(result.reply).level)
        if result.risk in {RiskLevel.high, RiskLevel.critical} and result.action == "send":
            result.action = "review"
        if _FALSE_HUMAN_CLAIM.search(result.reply):
            return _require_review(result, "回复包含不实的真人身份表述")
        return result

    if not _valid_candidates(result.candidates):
        result.selected_candidate = None
        result.risk = _max_risk(result.risk, incoming_risk, assess_risk(result.reply).level)
        return _require_review(result, "A/B/C 候选缺失、重复或无效，未另行请求模型")

    message = incoming_text(user_payload)
    ranked = []
    for candidate in result.candidates:
        # A model's low-risk label must not weaken deterministic local checks.
        candidate.risk = _max_risk(candidate.risk, assess_risk(candidate.reply).level)
        issues = _candidate_issues(candidate, message)
        key = (
            bool(issues),
            RISK_ORDER[candidate.risk],
            -candidate.boundary_respect,
            -candidate.context_fit,
            -candidate.style_match,
            -candidate.continuation,
            candidate.label,
        )
        ranked.append((key, candidate, issues))
    _, chosen, issues = min(ranked, key=lambda item: item[0])
    result.reply = chosen.reply.strip()
    result.selected_candidate = chosen.label
    result.risk = _max_risk(result.risk, incoming_risk, chosen.risk)
    _append_reason(result, f"本机按安全、边界、语境、本人表达、自然续聊顺序选择候选 {chosen.label}")
    if issues:
        return _require_review(result, "；".join(issues))
    if result.risk in {RiskLevel.high, RiskLevel.critical} and result.action == "send":
        result.action = "review"
    return result
