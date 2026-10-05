import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.codex_llm import AccountReplyLLM
from app.llm import LLMError
from app.models import RiskLevel


def test_account_inference_is_text_only_and_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.codex_llm.shutil.which", lambda _: "codex.exe")
    cfg = SimpleNamespace(paths=SimpleNamespace(database="db.sqlite3"),
                          openai=SimpleNamespace(timeout_seconds=30),
                          resolve=lambda _: tmp_path / "db.sqlite3")

    def invoke(command, **kwargs):
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert "shell_tool" in command and "multi_agent" in command and "apps" in command
        assert "incoming_data" in kwargs["input"]
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert set(schema["required"]) == set(schema["properties"])
        Path(command[command.index("-o") + 1]).write_text(json.dumps({
            "action": "review", "risk": "low", "reply": "收到。", "holding_reply": None,
            "reason": "日常确认", "confidence": .9, "facts_to_confirm": [], "memory_updates": []
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("app.codex_llm.subprocess.run", invoke)
    assert AccountReplyLLM(cfg).decide("rules", "hello", RiskLevel.low).reply == "收到。"
    assert not list((tmp_path / "inference_jobs").iterdir())


def test_account_errors_do_not_log_cli_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr("app.codex_llm.shutil.which", lambda _: "codex.exe")
    monkeypatch.setattr("app.codex_llm.subprocess.run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stderr="secret-message"))
    cfg = SimpleNamespace(paths=SimpleNamespace(database="db.sqlite3"),
                          openai=SimpleNamespace(timeout_seconds=30),
                          resolve=lambda _: tmp_path / "db.sqlite3")
    with pytest.raises(LLMError) as error:
        AccountReplyLLM(cfg).decide("rules", "hello", RiskLevel.low)
    assert "secret-message" not in str(error.value)
