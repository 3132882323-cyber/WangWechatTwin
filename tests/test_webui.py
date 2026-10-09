from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import Database
from app.models import IncomingMessage, ReplyDecision, RiskLevel
from app.webui import create_app


def test_health_detects_dead_reply_lane_and_stale_runtime(tmp_path):
    import time
    from app.config import AppConfig
    config = AppConfig(project_root=tmp_path)
    db = Database(tmp_path / 'health.sqlite3')
    client = TestClient(create_app(config, db))
    db.set_state('reply_scheduler_health', {'dead_lanes': ['doubao']})
    assert client.get('/health').json()['ok'] is False
    db.set_state('reply_scheduler_health', {'dead_lanes': []})
    db.set_state('runtime_heartbeat', {'seen_at': time.time() - 180})
    assert client.get('/health').json()['heartbeat_stale'] is True
    db.set_state('runtime_heartbeat', {'seen_at': time.time()})
    assert client.get('/health').json()['ok'] is True


def test_health_reports_required_model_bridge_heartbeats(tmp_path):
    import time
    from app.config import AppConfig

    config = AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'})
    db = Database(tmp_path / 'health.sqlite3')
    client = TestClient(create_app(config, db))
    db.set_state('runtime_heartbeat', {'seen_at': time.time()})
    db.set_state('reply_scheduler_health', {'dead_lanes': []})
    health = client.get('/health').json()
    assert health['runtime_ok'] is True
    assert health['model_bridges_ok'] is False
    assert health['ok'] is False
    assert {provider: bridge['status'] for provider, bridge in health['model_bridges'].items()} == {
        'chatgpt': 'missing', 'deepseek': 'missing', 'doubao': 'missing'}

    token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
    for provider in ('chatgpt', 'deepseek', 'doubao'):
        response = client.post('/browser-bridge/heartbeat', json={'blocked': False}, headers={
            'authorization': 'Bearer ' + token, 'x-wechat-bridge-provider': provider})
        assert response.status_code == 200
    health = client.get('/health').json()
    assert health['ok'] is True
    assert health['model_bridges_ok'] is True
    assert all(bridge['status'] == 'ready' for bridge in health['model_bridges'].values())

    state = db.get_state('doubao_bridge')
    db.set_state('doubao_bridge', {**state, 'heartbeat_seen_at': time.time() - 180,
                                   'seen_at': time.time()})
    health = client.get('/health').json()
    assert health['runtime_ok'] is True
    assert health['model_bridges_ok'] is False
    assert health['ok'] is False
    assert health['model_bridges']['doubao']['status'] == 'stale'
    assert health['model_bridges']['deepseek']['status'] == 'ready'

    client.post('/browser-bridge/heartbeat', json={'blocked': True}, headers={
        'authorization': 'Bearer ' + token, 'x-wechat-bridge-provider': 'doubao'})
    assert client.get('/health').json()['model_bridges']['doubao']['status'] == 'blocked'
    client.post('/browser-bridge/heartbeat', json={'blocked': False}, headers={
        'authorization': 'Bearer ' + token, 'x-wechat-bridge-provider': 'doubao'})
    assert client.get('/health').json()['ok'] is True


def test_bridge_poll_does_not_replace_explicit_heartbeat(tmp_path):
    import time
    from app.config import AppConfig

    for provider, mode in (('chatgpt', 'web'), ('deepseek', 'deepseek_web'), ('doubao', 'doubao_web')):
        root = tmp_path / provider
        config = AppConfig(project_root=root, openai={'provider': mode})
        db = Database(root / 'health.sqlite3')
        client = TestClient(create_app(config, db))
        token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
        headers = {'authorization': 'Bearer ' + token, 'x-wechat-bridge-provider': provider,
                   'x-wechat-bridge-version': '4'}
        key = 'browser_bridge' if provider == 'chatgpt' else provider + '_bridge'

        db.set_state(key, {'seen_at': time.time() - 180})
        assert client.get('/health').json()['model_bridges'][provider]['status'] == 'stale'
        assert client.get('/browser-bridge/next', headers=headers).status_code == 200
        assert client.get('/health').json()['model_bridges'][provider]['status'] == 'unconfirmed'
        client.post('/browser-bridge/heartbeat', json={'blocked': False}, headers=headers)
        pulse = db.get_state(key)['heartbeat_seen_at']
        assert client.get('/browser-bridge/next', headers=headers).status_code == 200
        state = db.get_state(key)
        assert state['heartbeat_seen_at'] == pulse
        assert client.get('/health').json()['ok'] is True

        db.set_state(key, {**state, 'heartbeat_seen_at': time.time() - 180})
        assert client.get('/browser-bridge/next', headers=headers).status_code == 200
        assert client.get('/health').json()['model_bridges'][provider]['status'] == 'stale'


def test_blocked_browser_heartbeat_never_claims_a_reply(tmp_path):
    from app.config import AppConfig
    from app.web_llm import WebReplyLLM
    import time
    config = AppConfig(project_root=tmp_path)
    config.openai.provider = 'hybrid_web'
    db = Database(tmp_path / 'review.sqlite3')
    client = TestClient(create_app(config, db))
    queue = WebReplyLLM(config)
    token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
    with queue.connect() as connection:
        connection.execute("INSERT INTO jobs(id,prompt,status,created,expires,provider) VALUES('blocked-check','test','pending',?,?,'doubao')", (time.time(), time.time()+60))
    assert client.post('/browser-bridge/heartbeat', json={'blocked': True}).status_code == 403
    response = client.post('/browser-bridge/heartbeat', json={'blocked': True}, headers={'authorization': 'Bearer '+token, 'x-wechat-bridge-provider': 'doubao'})
    assert response.status_code == 200
    assert db.get_state('doubao_bridge')['ready'] is False
    with queue.connect() as connection:
        assert connection.execute("SELECT status FROM jobs WHERE id='blocked-check'").fetchone()[0] == 'pending'


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

def test_hybrid_registers_authenticated_provider_queues(tmp_path):
    from app.config import AppConfig
    from app.web_llm import WebReplyLLM
    import time
    cfg=AppConfig(project_root=tmp_path);cfg.openai.provider='hybrid_web'
    cfg.paths.browser_bridge=str(tmp_path/'bridge');cfg.paths.database=str(tmp_path/'review.sqlite3')
    db=Database(cfg.resolve(cfg.paths.database));app=create_app(cfg,db);client=TestClient(app)
    token=(tmp_path/'bridge'/'pairing_token.txt').read_text().strip()
    assert client.get('/browser-bridge/next').status_code==403
    queue=WebReplyLLM(cfg)
    with queue.connect() as conn:
        for provider in ('chatgpt','deepseek','doubao'):
            conn.execute("INSERT INTO jobs(id,prompt,status,created,expires,provider) VALUES(?,?,'pending',?,?,?)",(provider,'test',time.time(),time.time()+60,provider))
    headers={'Authorization':'Bearer '+token,'X-Wechat-Bridge-Provider':'deepseek'}
    assert client.get('/browser-bridge/next',headers=headers).json()['job']['id']=='deepseek'
    headers['X-Wechat-Bridge-Provider']='chatgpt'
    assert client.get('/browser-bridge/next',headers=headers).json()['job']['id']=='chatgpt'


def test_doubao_route_registered_without_weakening_auth(tmp_path):
    from app.config import AppConfig
    cfg=AppConfig(project_root=tmp_path,openai={'provider':'hybrid_web'})
    cfg.paths.database=str(tmp_path/'db.sqlite3');cfg.paths.browser_bridge=str(tmp_path/'bridge')
    app=create_app(cfg,Database(cfg.resolve(cfg.paths.database)));client=TestClient(app)
    assert client.get('/browser-bridge/next',headers={'X-Wechat-Bridge-Provider':'doubao'}).status_code==403
    token=(tmp_path/'bridge'/'pairing_token.txt').read_text().strip()
    response=client.get('/browser-bridge/next',headers={'X-Wechat-Bridge-Provider':'doubao','Authorization':'Bearer '+token})
    assert response.status_code==200
