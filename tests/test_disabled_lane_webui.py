import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.db import Database
from app.web_llm import WebReplyLLM
from app.webui import create_app


@pytest.fixture
def disabled_bridge(tmp_path):
    config = AppConfig(project_root=tmp_path, adapter='mock', openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'],
        'local_proxies': {'deepseek': {
            'enabled': False, 'base_url': 'http://127.0.0.1:19655/v1', 'api_key_file': 'private/unused-key.txt',
        }},
    })
    db = Database(config.resolve(config.paths.database))
    app = create_app(config, db)
    token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
    headers = {'authorization': 'Bearer ' + token}
    with TestClient(app) as client:
        yield config, db, app, client, headers


def pending_recovery(db):
    previous = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', previous)
    return previous


def test_registration_keeps_old_recovery_evidence_without_reoffering_nonce(disabled_bridge, monkeypatch):
    config, db, app, client, headers = disabled_bridge
    previous = pending_recovery(db)
    identity = {'window_id': 11, 'host_tab_id': 22}
    calls = []

    def register(database, window_id, host_tab_id):
        calls.append((database, window_id, host_tab_id))
        return {'ok': True, 'title': 'WeChatTwin Background ' + 'b' * 32,
                'window_id': window_id, 'host_tab_id': host_tab_id}

    monkeypatch.setattr('app.browser_window.register', register)
    response = client.post('/browser-bridge/window/register', json=identity, headers=headers)

    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert 'recovery_requested' not in response.json()
    assert calls == [(db, 11, 22)]
    assert db.get_state('browser_model_recovery') == previous


def test_disabled_recovery_cannot_retire_registered_window(disabled_bridge, monkeypatch):
    config, db, app, client, headers = disabled_bridge
    previous = pending_recovery(db)
    identity = {'window_id': 11, 'host_tab_id': 22}
    db.set_state('browser_bridge_window', identity)
    calls = []

    def retire(*args):
        calls.append(args)
        return {'ok': True, 'retired': True}

    monkeypatch.setattr('app.browser_window.retire_for_recovery', retire)
    response = client.post('/browser-bridge/window/retire-for-recovery',
                           json={**identity, 'nonce': previous['nonce']}, headers=headers)

    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == previous
    assert db.get_state('browser_bridge_window') == identity


def test_disabled_recovery_cannot_acknowledge_old_matching_nonce(disabled_bridge):
    config, db, app, client, headers = disabled_bridge
    previous = pending_recovery(db)
    identity = {'window_id': 11, 'host_tab_id': 22}
    db.set_state('browser_bridge_window', identity)
    response = client.post('/browser-bridge/window/recovered',
                           json={**identity, 'nonce': previous['nonce']}, headers=headers)

    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') == previous


def test_owner_cannot_request_three_lane_recovery_while_deepseek_disabled(disabled_bridge, monkeypatch):
    config, db, app, client, headers = disabled_bridge
    previous = pending_recovery(db)
    calls = []
    monkeypatch.setattr('app.browser_window.resume', lambda database: calls.append(database) or {'ok': True})
    response = client.post('/browser-window/recover-models', data={'_csrf': app.state.csrf_token},
                           follow_redirects=False)

    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == previous
    assert db.get_state('browser_window_feedback') is None


@pytest.mark.parametrize('is_test', [0, 1], ids=['production', 'self-test'])
def test_disabled_deepseek_never_claims_stored_jobs(disabled_bridge, is_test):
    config, db, app, client, headers = disabled_bridge
    previous = {'ready': False, 'blocked': True, 'seen_at': 123}
    db.set_state('deepseek_bridge', previous)
    queue = WebReplyLLM(config)
    with closing(queue.connect()) as connection, connection:
        connection.execute(
            "INSERT INTO jobs(id,prompt,status,created,expires,provider,is_test) VALUES(?,?,'pending',?,?,'deepseek',?)",
            ('old-ds-job', 'fictional draft', time.time(), time.time() + 60, is_test),
        )

    response = client.get('/browser-bridge/next', headers={**headers, 'x-wechat-bridge-provider': 'deepseek'})

    assert response.status_code == 204
    with closing(queue.connect()) as connection:
        assert connection.execute("SELECT status FROM jobs WHERE id='old-ds-job'").fetchone()[0] == 'pending'
    assert db.get_state('deepseek_bridge') == previous


def test_health_does_not_require_or_probe_disabled_deepseek(disabled_bridge, monkeypatch):
    config, db, app, client, headers = disabled_bridge
    now = time.time()
    db.set_state('browser_bridge', {'ready': True, 'heartbeat_seen_at': now})
    db.set_state('doubao_bridge', {'ready': True, 'heartbeat_seen_at': now})
    previous = {'ready': False, 'blocked': True, 'heartbeat_seen_at': now}
    db.set_state('deepseek_bridge', previous)
    probed = []

    def unexpected_probe(*args, **kwargs):
        probed.append(args)
        raise AssertionError('Disabled local transport must not be probed')

    monkeypatch.setattr('socket.create_connection', unexpected_probe)
    response = client.get('/health')

    assert response.status_code == 200
    health = response.json()
    assert set(health['model_bridges']) == {'chatgpt', 'doubao'}
    assert health['runtime_ok'] is True
    assert health['model_bridges_ok'] is True
    assert health['ok'] is True
    assert probed == []
    assert db.get_state('deepseek_bridge') == previous
