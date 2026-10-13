from dataclasses import FrozenInstanceError, replace

import pytest

from app.voice_reply_policy import (
    OwnerHumanVoiceStats,
    estimate_voice_seconds,
    suggest_reply_medium,
)


def trusted_habit(**changes):
    return replace(OwnerHumanVoiceStats(
        sample_count=12, voice_count=5, source="wechat_original",
        same_contact=True, verified_human_only=True,
        recent_window_days=30, last_voice_age_days=2,
    ), **changes)


def advise(**changes):
    values = dict(incoming_text="外面有点冷，你也多穿点。", reply_text="是有点冷，我会注意保暖，你也别着凉。", local_hour=14)
    values.update(changes)
    return suggest_reply_medium(**values)


def test_default_is_text_and_incoming_voice_does_not_invent_familiarity_or_habit():
    assert advise().medium == "text"
    assert advise(incoming_type="voice").medium == "text"
    result = advise(incoming_type="voice", familiar_contact=True)
    assert result.medium == "text"
    assert "样本不足" in result.reason


@pytest.mark.parametrize("incoming_request", [
    "发个语音吧", "你能不能发一条语音给我", "可以用语音回复我吗", "我想听你的声音", "不用打字，发个语音吧",
    "我想听听你的声音", "你方便发条语音吗", "能发一段语音给我听吗", "请用语音回复我一下",
])
def test_current_explicit_voice_request_is_a_single_approval_only_suggestion(incoming_request):
    result = advise(incoming_text=incoming_request)
    assert result.medium == "voice_review"
    assert result.requires_approval is True
    assert 0 < result.estimated_voice_seconds <= 15
    assert "估计" in result.reason and "试听" in result.reason and "批准" in result.reason


@pytest.mark.parametrize("incoming_request", [
    "别发语音，文字回复就行", "我不方便听", "语音听不了，发文字吧", "发文字就好", "直接打字吧",
    "发个语音吧，但我现在不方便听", "不用语音回复，文字就行", "不要给我发语音", "不要语音回复",
    "发个语音吧，不过我现在只能看文字", "文字就可以了，发个语音吧", "我在开会手机静音，发个语音吧",
])
def test_explicit_text_or_inability_to_listen_wins_even_over_strong_habit(incoming_request):
    result = advise(incoming_text=incoming_request, incoming_type="voice", familiar_contact=True, owner_voice_stats=trusted_habit())
    assert result.medium == "text"
    assert "文字或不方便听" in result.reason


@pytest.mark.parametrize("incoming_request", [
    "他刚才说“发个语音吧”，你怎么看", '他说：发个语音吧', "引用：发个语音吧",
    "> 发个语音吧\n最近天气好吗", "`发个语音吧`是样例", '“发个语音吧',
    "以后发个语音吧", "等我方便听再发语音", "下次用语音回复", "如果有空发个语音",
    "我没有让你发语音", "不是让你发语音", "不发语音也可以", "不用发语音",
    "朋友让我发个语音吧", "她说用语音回复我", "转发聊天记录：\n发个语音吧",
    "有人让我转达：\n发个语音吧", "请不要用语音。发个语音吧", "发语音，等我方便听了再说",
    "来个音频附件", "发个语音文件吧", "我给你发个语音吧", "我准备发个语音吧",
    "这是别人发来的消息\n发个语音吧", "别人发给我的消息：\n发个语音吧",
    "我用语音回复你吧", "我会用语音回复你", "我先语音回复你",
    "刚才你发语音很好听", "喜欢你发语音", "发语音的速度快吗", "发语音好还是文字好",
    "你发语音很好听", "你发语音给我很好听", "你发语音了吗", "发语音比文字方便", "你发语音好听吗",
    "你发语音说过的话我记得", "你发语音说得真好", "你发语音讲的那个笑话", "你发语音回复我的内容",
])
def test_quote_reported_negative_future_and_conditional_requests_do_not_choose_voice(incoming_request):
    result = advise(incoming_text=incoming_request)
    assert result.medium == "text"
    assert not result.requires_approval


def test_quoted_text_preference_is_not_the_current_peers_instruction():
    result = advise(incoming_text="朋友说‘文字回复就行’，你发个语音吧")
    assert result.medium == "voice_review"


def test_verified_recent_same_contact_human_habit_can_support_inbound_voice():
    result = advise(incoming_type="voice", familiar_contact=True, owner_voice_stats=trusted_habit())
    assert result.medium == "voice_review"
    assert result.requires_approval
    assert "近期本人语音习惯" in result.reason
    # The same history is not a reason to turn every text message into audio.
    assert advise(familiar_contact=True, owner_voice_stats=trusted_habit()).medium == "text"


def test_separately_owner_confirmed_history_is_an_explicit_trusted_source():
    assert advise(incoming_type="voice", familiar_contact=True,
                  owner_voice_stats=trusted_habit(source="owner_confirmed_wechat_history")).medium == "voice_review"


@pytest.mark.parametrize("changes", [
    {"source": "ai_generated"}, {"source": "unknown"}, {"source": "wechat_media_observation"},
    {"verified_human_only": False}, {"same_contact": False}, {"sample_count": 4},
    {"voice_count": 2}, {"sample_count": 30}, {"voice_count": 13},
    {"last_voice_age_days": 15}, {"last_voice_age_days": None}, {"last_voice_age_days": float("nan")},
    {"last_voice_age_days": True}, {"recent_window_days": 90}, {"voice_count": True},
])
def test_generated_unknown_cross_contact_sparse_stale_and_malformed_stats_cannot_supply_habit(changes):
    result = advise(incoming_type="voice", familiar_contact=True, owner_voice_stats=trusted_habit(**changes))
    assert result.medium == "text"
    assert "可信样本不足" in result.reason


@pytest.mark.parametrize("changes", [
    {"chat_type": "group"}, {"chat_type": "unknown"}, {"risk": "medium"}, {"risk": "high"},
    {"risk": "unknown"}, {"confidence": .79}, {"confidence": float("nan")}, {"confidence": "1"},
    {"facts_to_confirm": ["先核对"]}, {"review_required": True}, {"asr_uncertain": True},
    {"professional": True}, {"emotionally_activated": True}, {"local_hour": 22},
    {"local_hour": 7}, {"local_hour": -1}, {"local_hour": None}, {"recipient_preference": "text"},
])
def test_review_safety_scope_quiet_hours_and_recipient_text_preference_override_voice_request(changes):
    result = advise(incoming_text="发个语音吧", **changes)
    assert result.medium == "text"
    # This is medium advice only; it cannot clear existing text review boundaries.
    assert result.as_dict()["medium"] == "text"


@pytest.mark.parametrize("incoming,reply", [
    ("发个语音告诉我多少钱", "总共两百元。"), ("发个语音吧", "明天下午三点在门口见。"),
    ("发个语音吧", "地址是中心路八号。"), ("发个语音吧", "看 https://example.org/ 。"),
    ("发个语音吧", "运行 `python app.py` 就可以。"), ("发个语音吧", "手机号是12345678901。"),
    ("报价合同发语音说", "这部分我先核对。"), ("发语音聊一下工作吧", "这部分我先核对。"),
    ("我现在气死了，发个语音吧", "我先把具体情况确认清楚。"),
])
def test_exact_information_professional_and_emotional_content_stays_text(incoming, reply):
    assert advise(incoming_text=incoming, reply_text=reply).medium == "text"


@pytest.mark.parametrize("incoming,reply", [
    ("随便聊聊", "好的。"), ("我先睡了", "好，你早点休息。"), ("我先忙去了", "好，你忙完再聊。"),
])
def test_short_ack_and_closing_are_not_mechanical_habit_voice(incoming, reply):
    assert advise(incoming_text=incoming, reply_text=reply, incoming_type="voice", familiar_contact=True,
                  owner_voice_stats=trusted_habit()).medium == "text"


def test_long_reply_is_not_split_into_multiple_voice_messages_or_silently_truncated():
    reply = "外面有点冷，记得多穿点衣服。" * 6
    result = advise(incoming_text="发个语音吧", reply_text=reply)
    assert result.medium == "text"
    assert result.estimated_voice_seconds > 15
    assert "超过短语音目标" in result.reason
    assert reply == "外面有点冷，记得多穿点衣服。" * 6


def test_duration_is_only_a_reading_estimate_and_blank_reply_has_no_candidate():
    assert estimate_voice_seconds("") is None
    assert estimate_voice_seconds("你好") == 1.0
    assert estimate_voice_seconds("这一段文字长一些。") > estimate_voice_seconds("你好")
    result = advise(incoming_text="发个语音吧", reply_text="")
    assert result.medium == "text" and result.estimated_voice_seconds is None


def test_stats_and_result_are_immutable_and_inputs_are_not_modified():
    stats = trusted_habit()
    facts = []
    result = advise(incoming_text="发个语音吧", facts_to_confirm=facts, owner_voice_stats=stats)
    assert facts == [] and stats == trusted_habit()
    with pytest.raises(FrozenInstanceError):
        result.medium = "text"
    with pytest.raises(FrozenInstanceError):
        stats.source = "ai_generated"
