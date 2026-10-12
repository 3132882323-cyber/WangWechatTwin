import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest

from app.config import AppConfig, load_config
from app.llm import LLMError, ReplyLLM
from app.models import ReplyDecision, RiskLevel


class FakeResponses:
    def parse(self, **kwargs):
        assert kwargs["text_format"] is ReplyDecision
        return SimpleNamespace(
            output_parsed=ReplyDecision(
                action="send",
                risk=RiskLevel.low,
                reply="在，说吧。",
                reason="普通问候",
                confidence=0.95,
            )
        )


class FakeOpenAI:
    def __init__(self, **kwargs):
        assert kwargs["api_key"] == "test-key"
        self.responses = FakeResponses()


def test_structured_llm_contract(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    raw["adapter"] = "mock"
    raw["paths"]["database"] = str(tmp_path / "db.sqlite3")
    raw["paths"]["pause_file"] = str(tmp_path / "PAUSE")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    llm = ReplyLLM(load_config(config_path))
    decision = llm.decide("system", "user", RiskLevel.low)
    assert decision.reply == "在，说吧。"


def test_text_falls_back_to_gpt_when_deepseek_page_is_dead(tmp_path):
    """A silent DeepSeek page must not leave a contact with no reply."""
    config = AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'})
    llm = ReplyLLM(config)
    calls = []

    class Stub:
        def __init__(self, lane):
            self.lane = lane

        def decide(self, system, payload, risk):
            calls.append(self.lane)
            if self.lane == 'deepseek':
                raise LLMError('任务未领取且已原子取消', fallback_safe=True)
            return ReplyDecision(action='send', risk=RiskLevel.low,
                                 reply='来自 GPT 的回复', confidence=.99)

    llm.hybrid = {name: Stub(name) for name in ('chatgpt', 'deepseek', 'doubao')}
    payload = json.dumps({'incoming': {'content': '在吗', 'message_type': 'text'}})
    result = llm._decide_once('sys', payload, RiskLevel.low)
    assert calls == ['deepseek', 'chatgpt']
    assert result.reply == '来自 GPT 的回复'


def test_high_risk_message_uses_gpt_without_fallback_confusion(tmp_path):
    """Risky messages still route to GPT first, never to the fast channel."""
    config = AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'})
    llm = ReplyLLM(config)
    assert llm.route(json.dumps({'incoming': {'content': '合同金额改一下', 'message_type': 'text'}}), RiskLevel.high) == 'chatgpt'
    assert llm.route(json.dumps({'incoming': {'content': '在吗', 'message_type': 'text'}}), RiskLevel.low) == 'deepseek'


@pytest.mark.parametrize('lane', ['deepseek', 'doubao'])
def test_unknown_submission_never_falls_back(tmp_path, lane):
    llm = ReplyLLM(AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web', 'everyday_lane': lane}))
    calls = []
    class Stub:
        def decide(self, *args):
            calls.append(lane)
            raise LLMError('提交结果未知')
    llm.hybrid = {lane: Stub(), 'chatgpt': Stub()}
    with pytest.raises(LLMError, match='提交结果未知'):
        llm._decide_once('sys', json.dumps({'incoming': {'content': '在吗'}}), RiskLevel.low)
    assert calls == [lane]


def test_doubao_safe_fallback_never_uses_disabled_deepseek(tmp_path):
    config = AppConfig(project_root=tmp_path, openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'],
    })
    llm = ReplyLLM(config)
    calls = []

    class Stub:
        def __init__(self, lane):
            self.lane = lane

        def decide(self, *args):
            calls.append(self.lane)
            if self.lane == 'doubao':
                raise LLMError('任务未领取且已原子取消', fallback_safe=True)
            return ReplyDecision(action='review', risk=RiskLevel.low, reply='测试')

    llm.hybrid = {name: Stub(name) for name in ('chatgpt', 'deepseek', 'doubao')}
    llm._decide_once('system', json.dumps({'incoming': {'content': '你吃饭了吗'}}), RiskLevel.low)
    assert calls == ['doubao', 'chatgpt']


def test_disabled_backup_is_not_called_even_if_injected(tmp_path):
    config = AppConfig(project_root=tmp_path, openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek', 'chatgpt'],
    })
    llm = ReplyLLM(config)
    calls = []

    class Stub:
        def decide(self, *args):
            calls.append('called')
            raise LLMError('任务未领取且已原子取消', fallback_safe=True)

    llm.hybrid = {'doubao': Stub(), 'chatgpt': Stub()}
    with pytest.raises(LLMError, match='任务未领取且已原子取消'):
        llm._decide_once('system', json.dumps({'incoming': {'content': '你吃饭了吗'}}), RiskLevel.low)
    assert calls == ['called']
    with pytest.raises(LLMError, match='model_lane_disabled'):
        llm._decide_once('system', json.dumps({'incoming': {'content': '分析施工方案'}}), RiskLevel.high)
    assert calls == ['called']


def test_missing_everyday_doubao_does_not_silently_return_to_deepseek(tmp_path):
    config = AppConfig(project_root=tmp_path, openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'],
    })
    llm = ReplyLLM(config)
    calls = []

    class Stub:
        def decide(self, *args):
            calls.append('deepseek')

    llm.hybrid = {'deepseek': Stub()}
    with pytest.raises(LLMError, match='model_lane_unavailable'):
        llm._decide_once('system', json.dumps({'incoming': {'content': '你吃饭了吗'}}), RiskLevel.low)
    assert calls == []


def test_disabled_web_provider_is_blocked_after_unvalidated_copy(tmp_path, monkeypatch):
    from app import web_llm
    config = AppConfig(project_root=tmp_path, openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'],
    })
    config.openai = config.openai.model_copy(update={'provider': 'deepseek_web'})
    constructed = []
    monkeypatch.setattr(web_llm, 'WebReplyLLM', lambda config: constructed.append(config))
    with pytest.raises(LLMError, match='model_lane_disabled'):
        ReplyLLM(config)
    assert constructed == []
