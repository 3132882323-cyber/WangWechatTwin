from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.webui import create_app


def test_dashboard_shows_incoming_and_learns_edit(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    raw["adapter"] = "mock"
    raw["mode"] = "low_risk_auto"
    raw["paths"]["database"] = str(tmp_path / "db.sqlite3")
    raw["paths"]["pause_file"] = str(tmp_path / "PAUSE")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    config = load_config(config_path)
    db = Database(config.resolve(config.paths.database))
    mid = db.add_incoming(
        IncomingMessage(external_id="w1", contact="张三", sender="张三", content="能便宜吗"),
        risk="high",
    )
    did = db.create_draft(
        "张三",
        mid,
        ReplyDecision(
            action="review",
            risk=RiskLevel.high,
            reply="我核一下。",
            reason="报价需确认",
            confidence=0.7,
        ),
    )
    app = create_app(config, db)
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "能便宜吗" in page.text
    response = client.post(
        f"/draft/{did}/approve",
        data={"reply": "你把数量和配置发我，我按实际给你核。", "_csrf": app.state.csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.approved_drafts()[0].edited_reply.startswith("你把数量")
    assert db.style_feedback("张三")
    assert client.post(f"/draft/{did}/approve", data={"reply": "untrusted post"}).status_code == 403
    config.mode = "shadow"
    assert client.post(f"/draft/{did}/approve", data={"reply": "不应发送"}).status_code == 403
