import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.config import load_config
from app.llm import ReplyLLM
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
