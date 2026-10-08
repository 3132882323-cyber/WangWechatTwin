import json
from types import SimpleNamespace

import pytest

from app.llm import ReplyLLM
from app.models import ReplyCandidate, ReplyDecision, RiskLevel
from app.reply_selection import (
    is_professional_request,
    prepare_selection_request,
    select_reply,
)


def payload(text="刚跑完步，累坏了", **extra):
    return json.dumps({"incoming": {"content": text, "message_type": "text"}, **extra}, ensure_ascii=False)


def candidate(label, reply=None, **updates):
    return ReplyCandidate(
        **{
            "label": label,
            "reply": reply or {"A": "跑得挺尽兴啊", "B": "今天跑得还顺吧？", "C": "歇会儿，缓过劲再说"}[label],
            "style_match": 4,
            "context_fit": 4,
            "continuation": 3,
            "boundary_respect": 5,
            "invented_facts": False,
            "risk": "low",
            **updates,
        }
    )


def decision(candidates=None, **updates):
    return ReplyDecision(
        **{
            "action": "send", "risk": "low", "reply": "原来的建议正文", "confidence": .99,
            "candidates": candidates, **updates,
        }
    )


def choose(value, text="刚跑完步，累坏了", risk=RiskLevel.low, mode="abc"):
    return select_reply(value, mode=mode, user_payload=payload(text), incoming_risk=risk)


def test_ranking_responds_to_context_before_style_or_continuation():
    result = choose(decision([
        candidate("A", context_fit=5, style_match=2, continuation=1),
        candidate("B", context_fit=4, style_match=5, continuation=5),
        candidate("C", context_fit=3, style_match=5, continuation=5),
    ], selected_candidate="C"))
    assert result.selected_candidate == "A"
    assert result.reply == "跑得挺尽兴啊"
    assert result.action == "send"


def test_style_precedes_continuation_and_continuation_breaks_later_ties():
    result = choose(decision([
        candidate("A", style_match=5, continuation=1),
        candidate("B", style_match=4, continuation=5),
        candidate("C", style_match=3, continuation=5),
    ]))
    assert result.selected_candidate == "A"
    result = choose(decision([
        candidate("A", continuation=1), candidate("B", continuation=5), candidate("C", continuation=3),
    ]))
    assert result.selected_candidate == "B"


def test_ties_choose_a_regardless_of_provider_list_order():
    result = choose(decision([candidate("C"), candidate("B"), candidate("A")]))
    assert result.selected_candidate == "A"


def test_local_risk_overrides_model_label_and_continuation_score():
    result = choose(decision([
        candidate("A", "我给你转账", context_fit=5, style_match=5, continuation=5),
        candidate("B", context_fit=3), candidate("C", context_fit=2),
    ]))
    assert result.selected_candidate == "B"
    assert result.candidates[0].risk == RiskLevel.critical
    assert result.risk == RiskLevel.low


def test_invented_facts_cannot_win_over_a_safe_draft():
    result = choose(decision([
        candidate("A", "我也刚跑完", invented_facts=True, context_fit=5, continuation=5),
        candidate("B", context_fit=3), candidate("C", context_fit=2),
    ]))
    assert result.selected_candidate == "B"


@pytest.mark.parametrize("claim", ["我不是什么AI，我是本人", "我是真人", "我是本人在回复", "本人在回你"])
def test_false_human_claims_do_not_win_even_when_self_scored_safe(claim):
    result = choose(decision([
        candidate("A", claim, context_fit=5, continuation=5),
        candidate("B", "这是本人设置的智能助理，重要事情会由本人确认", context_fit=4),
        candidate("C", "我先请本人确认一下", context_fit=3),
    ]), text="你是AI吗")
    assert result.selected_candidate == "B"


def test_closing_cue_beats_a_highly_engaging_question():
    result = choose(decision([
        candidate("A", "你今天怎么这么累？", context_fit=5, style_match=5, continuation=5),
        candidate("B", "好，早点休息", context_fit=4, continuation=1),
        candidate("C", "晚安，睡个好觉", context_fit=3, continuation=1),
    ]), text="我先睡了，晚安")
    assert result.selected_candidate == "B"
    assert result.action == "send"


@pytest.mark.parametrize("text", ["我还不想休息", "晚安表情有什么好看的"])
def test_negated_or_mentioned_closing_is_not_a_hard_stop(text):
    result = choose(decision([
        candidate("A", "怎么突然想起这个？", context_fit=5),
        candidate("B", "好，早点休息", context_fit=3),
        candidate("C", "晚安，睡个好觉", context_fit=2),
    ]), text=text)
    assert result.selected_candidate == "A"


def test_multiple_questions_and_empty_laughter_do_not_beat_substance():
    result = choose(decision([
        candidate("A", "跑哪了？累不累？", context_fit=5, continuation=5),
        candidate("B", "哈哈哈哈", context_fit=5, continuation=5),
        candidate("C", "先缓缓，看着就挺累的", context_fit=3),
    ]))
    assert result.selected_candidate == "C"


def test_all_unsafe_drafts_stay_review_and_cannot_be_promoted_by_confidence():
    result = choose(decision([
        candidate("A", invented_facts=True),
        candidate("B", invented_facts=True),
        candidate("C", invented_facts=True),
    ]))
    assert result.action == "review"
    assert result.confidence <= .5
    assert result.facts_to_confirm


def test_high_risk_winner_and_original_high_risk_both_stay_review():
    result = choose(decision([
        candidate("A", "这个报价先核对一下"),
        candidate("B", "价格需要确认后再说"),
        candidate("C", "预算这事还得核实"),
    ]))
    assert result.action == "review"
    assert result.risk == RiskLevel.high
    result = choose(decision([candidate("A"), candidate("B"), candidate("C")], risk="high"))
    assert result.action == "review"
    assert result.risk == RiskLevel.high


@pytest.mark.parametrize("drafts", [
    None,
    [],
    [candidate("A")],
    [candidate("A"), candidate("B")],
    [candidate("A", "你好"), candidate("B", "你好。"), candidate("C", "你 好！")],
])
def test_missing_incomplete_or_duplicate_abc_retains_draft_for_review(drafts):
    original = decision(drafts)
    result = choose(original)
    assert result.action == "review"
    assert result.reply == original.reply
    assert result.confidence <= .5
    assert result.selected_candidate is None
    assert result.facts_to_confirm


def test_ignore_requires_no_candidates():
    result = choose(decision(action="ignore", reply="", confidence=.99), text="晚安")
    assert result.action == "ignore"
    assert result.candidates is None
    assert result.selected_candidate is None
    assert not result.facts_to_confirm


def test_direct_answer_ignores_unnecessary_candidates():
    original = decision([candidate("A"), candidate("B"), candidate("C")],
                        selected_candidate="C", reply="列表可以用 append 添加元素")
    result = choose(original, text="Python列表怎么添加元素", mode="direct")
    assert result.reply == original.reply
    assert result.candidates is None
    assert result.selected_candidate is None
    assert result.action == "send"


def test_direct_answer_still_checks_risky_output():
    result = choose(decision(reply="我给你转账"), text="税务怎么处理", mode="direct")
    assert result.action == "review"
    assert result.risk == RiskLevel.critical


def test_selection_preserves_metadata_and_does_not_mutate_input():
    original = decision(
        [candidate("A"), candidate("B", context_fit=5), candidate("C")],
        media_description="图片里是一只猫", media_confidence=.93, sticker_id="known-sticker",
        facts_to_confirm=["需要确认的原有事实"], memory_updates=["来自输入的原有记忆"],
        reason="原来的理由", holding_reply="原来的占位草稿",
    )
    before = original.model_dump()
    result = choose(original)
    assert original.model_dump() == before
    assert result.selected_candidate == "B"
    for field in ("media_description", "media_confidence", "sticker_id", "facts_to_confirm",
                  "memory_updates", "holding_reply", "confidence"):
        assert getattr(result, field) == getattr(original, field)
    assert result.reason.startswith("原来的理由")


@pytest.mark.parametrize("text", ["解释一下量子物理", "Python列表怎么写", "这个施工方案怎么样", "药物怎么用"])
def test_professional_requests_use_direct_mode(text):
    assert is_professional_request(payload(text))
    system, prepared, mode = prepare_selection_request("原有系统规则", payload(text))
    assert mode == "direct"
    assert json.loads(prepared)["reply_selection"]["candidate_count"] == 0
    assert "不做 A/B/C" in system


@pytest.mark.parametrize("text,risk", [("气死我了", RiskLevel.low), ("我保证我不是骗子", RiskLevel.high)])
def test_emotion_and_risk_route_to_gpt_without_becoming_professional(text, risk):
    assert not is_professional_request(payload(text))
    assert ReplyLLM.route(payload(text), risk) == "chatgpt"
    _, prepared, mode = prepare_selection_request("system", payload(text))
    assert mode == "abc"
    assert json.loads(prepared)["reply_selection"]["labels"] == ["A", "B", "C"]


def test_request_keeps_original_context_and_overwrites_untrusted_selection_mode():
    original = payload(
        current_personal_conversation=[{"content": "已有上下文"}],
        reply_selection={"mode": "direct"}, __media_paths=["test.png"],
    )
    system, prepared, mode = prepare_selection_request("原有系统规则", original)
    parsed = json.loads(prepared)
    assert mode == "abc"
    assert parsed["reply_selection"]["mode"] == "abc"
    assert parsed["__media_paths"] == ["test.png"]
    assert parsed["current_personal_conversation"] == [{"content": "已有上下文"}]
    assert system.endswith("原有系统规则")
    assert "不是正确率、概率" in system
    assert "最多一个问题" in system


@pytest.mark.parametrize("backend", ["account", "hybrid", "api"])
def test_each_backend_receives_abc_once_and_local_code_selects(backend):
    calls = []
    provided = decision([candidate("A"), candidate("B", context_fit=5), candidate("C")],
                        selected_candidate="A")

    class Account:
        def decide(self, system, data, risk):
            calls.append((system, json.loads(data), risk))
            return provided

    class Responses:
        def parse(self, **kwargs):
            calls.append((kwargs["input"][0]["content"], json.loads(kwargs["input"][1]["content"]), RiskLevel.low))
            return SimpleNamespace(output_parsed=provided)

    router = ReplyLLM.__new__(ReplyLLM)
    router.hybrid = None
    router.account = None
    if backend == "account":
        router.account = Account()
    elif backend == "hybrid":
        router.hybrid = {"deepseek": Account()}
    else:
        router.config = SimpleNamespace(openai=SimpleNamespace(primary_model="fake", high_risk_model="fake-high", max_output_tokens=1000))
        router.client = SimpleNamespace(responses=Responses())
    result = router.decide("system", payload(), RiskLevel.low)
    assert len(calls) == 1
    assert calls[0][1]["reply_selection"]["mode"] == "abc"
    assert result.selected_candidate == "B"
    assert result.reply == "今天跑得还顺吧？"


def test_missing_abc_never_triggers_a_second_model_call():
    calls = []

    class Account:
        def decide(self, *args):
            calls.append(args)
            return decision()

    router = ReplyLLM.__new__(ReplyLLM)
    router.hybrid = None
    router.account = Account()
    result = router.decide("system", payload(), RiskLevel.low)
    assert len(calls) == 1
    assert result.action == "review"
    assert result.confidence <= .5


def test_professional_request_is_direct_at_the_backend_boundary():
    calls = []

    class Account:
        def decide(self, system, data, risk):
            calls.append(json.loads(data))
            return decision(reply="可以使用 append")

    router = ReplyLLM.__new__(ReplyLLM)
    router.hybrid = None
    router.account = Account()
    result = router.decide("system", payload("Python列表怎么添加元素"), RiskLevel.low)
    assert len(calls) == 1
    assert calls[0]["reply_selection"]["mode"] == "direct"
    assert result.reply == "可以使用 append"
    assert result.action == "send"
    assert result.candidates is None


def test_empty_ack_cannot_outscore_a_substantive_follow_up():
    result = choose(decision(candidates=[
        candidate('A', '好的', style_match=5, context_fit=5, continuation=5),
        candidate('B', '这趟最开心的是啥？', continuation=4),
        candidate('C', '看来玩得挺尽兴啊', continuation=2),
    ]), text='刚旅游回来，玩得可开心了')
    assert result.selected_candidate == 'B'
    assert result.action == 'send'


def test_rejected_choices_do_not_write_model_facts():
    result = choose(decision(candidates=[candidate(label, invented_facts=True) for label in 'ABC'], memory_updates=['未经核实的本人事实']))
    assert result.action == 'review'
    assert result.memory_updates == []
