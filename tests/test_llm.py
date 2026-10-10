import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

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


def test_unknown_submission_never_falls_back(tmp_path):
    import pytest
    llm = ReplyLLM(AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'}))
    calls = []
    class Stub:
        def decide(self, *args):
            calls.append('deepseek')
            raise LLMError('提交结果未知')
    llm.hybrid = {'deepseek': Stub(), 'chatgpt': Stub()}
    with pytest.raises(LLMError, match='提交结果未知'):
        llm._decide_once('sys', json.dumps({'incoming': {'content': '在吗'}}), RiskLevel.low)
    assert calls == ['deepseek']
