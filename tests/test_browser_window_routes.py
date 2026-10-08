import re

import pytest
from fastapi.testclient import TestClient
from app.config import AppConfig
from app.db import Database
from app.webui import create_app


def setup(tmp_path):
    config = AppConfig(project_root=tmp_path)
    config.openai.provider = 'hybrid_web'
    db = Database(tmp_path / 'review.sqlite3')
    app = create_app(config, db)
    token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
    return db, app, TestClient(app), {'authorization': 'Bearer '+token}


def test_window_registration_requires_pairing_and_hide_requires_exact_identity(tmp_path, monkeypatch):
    db, app, client, headers = setup(tmp_path)
    body = {'window_id': 11, 'host_tab_id': 22}
    assert client.post('/browser-bridge/window/register', json=body).status_code == 403
    response = client.post('/browser-bridge/window/register', json=body, headers=headers)
    assert response.status_code == 200
    registration = response.json()
    calls = []
    monkeypatch.setattr('app.browser_window.hide', lambda database: calls.append(True) or {'ok': True, 'hidden': True, 'verified': True})
    assert client.post('/browser-bridge/window/hide', json={**body, 'title': 'another window'}, headers=headers).status_code == 409
    assert calls == []
    assert client.post('/browser-bridge/window/hide', json={**body, 'title': registration['title']}, headers=headers).status_code == 200
    assert calls == [True]


def test_window_manual_controls_require_owner_csrf(tmp_path, monkeypatch):
    db, app, client, headers = setup(tmp_path)
    assert client.post('/browser-window/show').status_code == 403
    assert client.post('/browser-window/resume').status_code == 403
    monkeypatch.setattr('app.browser_window.show', lambda database: {'ok': True, 'manual_reveal': True})
    response = client.post('/browser-window/show', data={'_csrf': app.state.csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert '后台网页已显示' in db.get_state('browser_window_feedback')['message']


@pytest.mark.parametrize('authorization', ['missing', 'incorrect', 'pairing_only'])
def test_model_recovery_request_requires_owner_csrf_before_mutation(tmp_path, monkeypatch, authorization):
    db, app, client, headers = setup(tmp_path)
    previous = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', previous)
    calls = []
    monkeypatch.setattr('app.browser_window.resume', lambda database: calls.append(True) or {'ok': True})

    response = client.post(
        '/browser-window/recover-models',
        data={'_csrf': 'incorrect'} if authorization == 'incorrect' else {},
        headers=headers if authorization == 'pairing_only' else {},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert calls == []
    assert db.get_state('browser_model_recovery') == previous
    assert db.get_state('browser_window_feedback') is None


def test_http_host_owner_form_requests_recovery_and_returns_to_same_host(tmp_path):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    registration = client.post('/browser-bridge/window/register', json=identity, headers=headers).json()
    state = db.get_state('browser_bridge_window')
    state['manual_reveal'] = True
    db.set_state('browser_bridge_window', state)

    host = client.get('/browser-window/host')
    assert host.status_code == 200
    assert host.headers['cache-control'] == 'no-store'
    assert host.headers['x-frame-options'] == 'DENY'
    assert 'action="/browser-window/recover-models"' in host.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', host.text).group(1)
    assert headers['authorization'].removeprefix('Bearer ') not in host.text
    assert registration['title'] not in host.text

    response = client.post('/browser-window/recover-models', data={'_csrf': csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers['location'] == '/browser-window/host'
    assert db.get_state('browser_bridge_window')['manual_reveal'] is False
    recovery = db.get_state('browser_model_recovery')
    assert re.fullmatch(r'[0-9a-f]{32}', recovery['nonce'])
    assert recovery['requested_at'] > 0
    assert recovery['completed'] is False
    assert db.get_state('browser_window_feedback')['ok'] is True
    registered = client.post('/browser-bridge/window/register', json=identity, headers=headers).json()
    assert registered['recovery_requested'] == {'nonce': recovery['nonce']}


@pytest.mark.parametrize('authorization', [None, 'Bearer incorrect'])
def test_recovery_acknowledgement_requires_pairing_without_mutation(tmp_path, authorization):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    client.post('/browser-bridge/window/register', json=identity, headers=headers)
    recovery = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', recovery)

    response = client.post(
        '/browser-bridge/window/recovered',
        json={**identity, 'nonce': recovery['nonce']},
        headers={'authorization': authorization} if authorization else {},
    )

    assert response.status_code == 403
    assert db.get_state('browser_model_recovery') == recovery


@pytest.mark.parametrize('field', ['nonce', 'window_id', 'host_tab_id'])
@pytest.mark.parametrize('missing', [False, True], ids=['different', 'missing'])
def test_recovery_acknowledgement_binds_nonce_and_both_window_ids(tmp_path, field, missing):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    client.post('/browser-bridge/window/register', json=identity, headers=headers)
    recovery = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', recovery)
    body = {**identity, 'nonce': recovery['nonce']}
    if missing:
        del body[field]
    else:
        body[field] = 'b' * 32 if field == 'nonce' else body[field] + 1

    response = client.post('/browser-bridge/window/recovered', json=body, headers=headers)

    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') == recovery


def test_recovery_acknowledgement_requires_a_registered_window(tmp_path):
    db, app, client, headers = setup(tmp_path)
    recovery = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', recovery)

    response = client.post(
        '/browser-bridge/window/recovered',
        json={'nonce': recovery['nonce'], 'window_id': None, 'host_tab_id': None},
        headers=headers,
    )

    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') == recovery


def test_recovery_acknowledgement_requires_an_active_recovery_request(tmp_path):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    client.post('/browser-bridge/window/register', json=identity, headers=headers)

    response = client.post('/browser-bridge/window/recovered', json={**identity, 'nonce': 'a' * 32}, headers=headers)

    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') is None


def test_successful_recovery_acknowledgement_echoes_identity_and_stops_reoffering_nonce(tmp_path):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    client.post('/browser-bridge/window/register', json=identity, headers=headers)
    response = client.post('/browser-window/recover-models', data={'_csrf': app.state.csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    recovery = db.get_state('browser_model_recovery')
    body = {**identity, 'nonce': recovery['nonce']}

    acknowledged = client.post('/browser-bridge/window/recovered', json=body, headers=headers)

    assert acknowledged.status_code == 200
    assert acknowledged.json() == {'ok': True, **body}
    completed = db.get_state('browser_model_recovery')
    assert completed['nonce'] == recovery['nonce']
    assert completed['requested_at'] == recovery['requested_at']
    assert completed['completed'] is True
    assert completed['completed_at'] >= recovery['requested_at']
    registered = client.post('/browser-bridge/window/register', json=identity, headers=headers).json()
    assert 'recovery_requested' not in registered


def test_old_recovery_acknowledgement_cannot_complete_a_new_request(tmp_path):
    db, app, client, headers = setup(tmp_path)
    identity = {'window_id': 11, 'host_tab_id': 22}
    client.post('/browser-bridge/window/register', json=identity, headers=headers)
    for _ in range(2):
        previous = db.get_state('browser_model_recovery')
        response = client.post('/browser-window/recover-models', data={'_csrf': app.state.csrf_token}, follow_redirects=False)
        assert response.status_code == 303
    current = db.get_state('browser_model_recovery')
    assert previous['nonce'] != current['nonce']

    response = client.post('/browser-bridge/window/recovered', json={**identity, 'nonce': previous['nonce']}, headers=headers)

    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') == current
