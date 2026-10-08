import pytest
from pydantic import ValidationError

from app.models import ReplyCandidate, ReplyDecision, RiskLevel


def candidate(label="A", reply="这事听着挺费劲，后来怎么处理的？", **updates):
    payload = {
        "label": label,
        "reply": reply,
        "style_match": 4,
        "context_fit": 5,
        "continuation": 4,
        "boundary_respect": 5,
        "invented_facts": False,
        "risk": "low",
    }
    payload.update(updates)
    return payload


def decision(**updates):
    payload = {"action": "send", "risk": "low", "reply": "在，说吧。"}
    payload.update(updates)
    return ReplyDecision.model_validate(payload)


def test_legacy_decision_without_candidates_remains_valid():
    result = decision()
    assert result.reply == "在，说吧。"
    assert result.confidence == 0.5
    assert result.candidates is None
    assert result.selected_candidate is None


def test_three_candidates_and_selection_round_trip():
    result = decision(
        candidates=[candidate(label) for label in ("A", "B", "C")],
        selected_candidate="B",
    )
    restored = ReplyDecision.model_validate_json(result.model_dump_json())
    assert [draft.label for draft in restored.candidates] == ["A", "B", "C"]
    assert restored.selected_candidate == "B"
    assert restored.candidates[1].risk == RiskLevel.low


@pytest.mark.parametrize("value", [-1, 6, 2.5, True, "4"])
@pytest.mark.parametrize(
    "score", ["style_match", "context_fit", "continuation", "boundary_respect"]
)
def test_candidate_scores_require_integers_in_ordinal_range(score, value):
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate(candidate(**{score: value}))


@pytest.mark.parametrize("value", [0, 5])
def test_candidate_scores_accept_both_endpoints(value):
    result = ReplyCandidate.model_validate(candidate(
        style_match=value, context_fit=value,
        continuation=value, boundary_respect=value,
    ))
    assert result.style_match == value
    assert result.boundary_respect == value


@pytest.mark.parametrize("reply", ["", " \t\n", "字" * 601])
def test_candidate_reply_rejects_empty_or_oversized_drafts(reply):
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate(candidate(reply=reply))


def test_candidate_reply_accepts_exact_length_limit():
    result = ReplyCandidate.model_validate(candidate(reply="字" * 600))
    assert len(result.reply) == 600


@pytest.mark.parametrize("label", ["D", "a", 1])
def test_candidate_label_is_limited_to_abc(label):
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate(candidate(label=label))


def test_decision_rejects_more_than_three_candidates():
    with pytest.raises(ValidationError):
        decision(candidates=[candidate(label) for label in ("A", "B", "C", "A")])


def test_decision_rejects_duplicate_candidate_labels():
    with pytest.raises(ValidationError, match="candidate labels must be unique"):
        decision(candidates=[candidate("A"), candidate("A")])


@pytest.mark.parametrize("candidates", [None, [], [candidate("A")]])
def test_selection_must_refer_to_an_available_candidate(candidates):
    with pytest.raises(ValidationError, match="provided candidate"):
        decision(candidates=candidates, selected_candidate="B")


def test_candidates_can_be_retained_before_selection():
    result = decision(candidates=[candidate("C")])
    assert result.selected_candidate is None
    assert result.candidates[0].label == "C"


@pytest.mark.parametrize("value", ["false", 0, None])
def test_invented_facts_requires_a_boolean(value):
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate(candidate(invented_facts=value))


def test_candidate_risk_reuses_existing_risk_levels():
    result = ReplyCandidate.model_validate(candidate(invented_facts=True, risk="high"))
    assert result.invented_facts is True
    assert result.risk == RiskLevel.high
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate(candidate(risk="safe"))


def test_json_schema_exposes_bounds_and_subjective_scoring():
    schema = ReplyDecision.model_json_schema()
    candidate_schema = schema["$defs"]["ReplyCandidate"]["properties"]
    assert candidate_schema["reply"]["maxLength"] == 600
    assert candidate_schema["style_match"]["minimum"] == 0
    assert candidate_schema["style_match"]["maximum"] == 5
    assert "not a calibrated probability" in candidate_schema["style_match"]["description"]
    candidates_schema = schema["properties"]["candidates"]["anyOf"]
    assert next(item for item in candidates_schema if item.get("type") == "array")["maxItems"] == 3
