from dataclasses import dataclass
from pathlib import Path

import yaml

from app.adapters.wxauto_adapter import WxAutoAdapter
from app.config import load_config


@dataclass
class FakeMsg:
    id: str
    attr: str
    type: str
    sender: str
    content: str


class FakeFreeWx:
    def __init__(self):
        self.current = ""
        self.messages = {
            "张三": [FakeMsg("1", "friend", "text", "张三", "历史消息")]
        }
        self.sent = []

    def ChatWith(self, who=None, exact=True):
        self.current = who

    def ChatInfo(self):
        return {"chat_name": self.current, "chat_type": "friend"}

    def GetAllMessage(self):
        return list(self.messages.get(self.current, []))

    def SendMsg(self, msg, **kwargs):
        self.sent.append((self.current, msg))
        return True


def make_config(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    raw["paths"]["database"] = str(tmp_path / "db.sqlite3")
    raw["paths"]["pause_file"] = str(tmp_path / "PAUSE")
    raw["contacts"] = [{"name": "张三", "relationship": "客户", "mode": "inherit"}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def test_free_adapter_baseline_then_new_message(tmp_path: Path):
    config = make_config(tmp_path)
    adapter = WxAutoAdapter(config, initialize=False)
    adapter.wx = FakeFreeWx()
    adapter.variant = "test-free"

    assert adapter.poll() == []  # Old history is baseline only.
    adapter.wx.messages["张三"].append(FakeMsg("2", "friend", "text", "张三", "在吗"))
    messages = adapter.poll()
    assert len(messages) == 1
    assert messages[0].content == "在吗"
    assert messages[0].contact == "张三"


def test_free_adapter_ignores_self_message(tmp_path: Path):
    config = make_config(tmp_path)
    adapter = WxAutoAdapter(config, initialize=False)
    adapter.wx = FakeFreeWx()
    adapter.poll()
    adapter.wx.messages["张三"].append(FakeMsg("2", "self", "text", "我", "我发的"))
    assert adapter.poll() == []
