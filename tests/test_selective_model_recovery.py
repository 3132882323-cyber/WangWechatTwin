import re

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.db import Database
from app.webui import create_app


IDENTITY = {'window_id': 11, 'host_tab_id': 22}


@pytest.fixture
def selective_bridge(tmp_path):
    config = AppConfig(project_root=tmp_path, adapter='mock', openai={
        'provider': 'hybrid_web', 'everyday_lane': 'glm', 'disabled_lanes': ['deepseek'],
        'local_proxies': {
            'glm': {'enabled': True, 'base_url': 'http://127.0.0.1:19659/v1',
                    'api_key_file': 'private/glm-key.txt'},
            'doubao': {'enabled': True, 'base_url': 'http://127.0.0.1:19656/v1',
                       'api_key_file': 'private/doubao-key.txt'},
        },
    })
    db = Database(config.resolve(config.paths.database))
    app = create_app(config, db)
    token = (config.resolve(config.paths.browser_bridge) / 'pairing_token.txt').read_text().strip()
    with TestClient(app) as client:
        yield config, db, app, client, {'authorization': 'Bearer ' + token}


def register(bridge, identity=IDENTITY, **extra):
    config, db, app, client, headers = bridge
    return client.post('/browser-bridge/window/register', json={**identity, **extra}, headers=headers)


def request_recovery(bridge):
    config, db, app, client, headers = bridge
    response = client.post('/browser-window/recover-selected-models',
                           data={'_csrf': app.state.csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/browser-window/host'
    return db.get_state('browser_model_recovery')


def recovery_body(recovery, identity=IDENTITY):
    return {**identity, 'nonce': recovery['nonce'], 'version': 2, 'providers': recovery['providers']}


def test_registration_advertises_only_required_web_transport(selective_bridge):
    response = register(selective_bridge)
    assert response.status_code == 200
    assert response.json()['eligible_web'] == ['chatgpt']
    assert 'recovery_requested' not in response.json()


@pytest.mark.parametrize('authorization', ['missing', 'incorrect', 'pairing_only'])
def test_selective_owner_request_requires_csrf_before_resume_or_state_change(selective_bridge, monkeypatch, authorization):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    old = {'nonce': 'a' * 32, 'completed': False}
    db.set_state('browser_model_recovery', old)
    calls = []
    monkeypatch.setattr('app.browser_window.resume', lambda database: calls.append(database))
    response = client.post('/browser-window/recover-selected-models',
                           data={'_csrf': 'incorrect'} if authorization == 'incorrect' else {},
                           headers=headers if authorization == 'pairing_only' else {}, follow_redirects=False)
    assert response.status_code == 403
    assert calls == []
    assert db.get_state('browser_model_recovery') == old
    assert db.get_state('browser_window_feedback') is None


def test_owner_request_clears_manual_reveal_and_binds_scope_and_registration(selective_bridge):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    window = db.get_state('browser_bridge_window')
    window['manual_reveal'] = True
    db.set_state('browser_bridge_window', window)
    recovery = request_recovery(selective_bridge)
    assert re.fullmatch('[0-9a-f]{32}', recovery['nonce'])
    assert recovery['version'] == 2
    assert recovery['providers'] == ['chatgpt']
    assert recovery['completed'] is False
    assert recovery['requested_at'] > 0
    assert {key: recovery[key] for key in IDENTITY} == IDENTITY
    assert recovery['registration_pending'] is False
    assert db.get_state('browser_bridge_window')['manual_reveal'] is False
    assert config.openai.disabled_lanes == ['deepseek']
    assert config.openai.local_proxies['doubao'].enabled is True


def test_selective_http_host_uses_one_click_owner_form(selective_bridge):
    config, db, app, client, headers = selective_bridge
    response = client.get('/browser-window/host')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-frame-options'] == 'DENY'
    assert 'action="/browser-window/recover-selected-models"' in response.text
    assert '当前网页模型：GPT' in response.text
    assert 'action="/browser-window/recover-models"' not in response.text
    assert headers['authorization'].removeprefix('Bearer ') not in response.text


@pytest.mark.parametrize('version', [None, 1, 3, '2', True])
def test_selective_nonce_is_never_offered_to_legacy_or_invalid_version(selective_bridge, version):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    extra = {} if version is None else {'recovery_version': version}
    response = register(selective_bridge, **extra)
    assert response.json()['eligible_web'] == ['chatgpt']
    assert 'recovery_requested' not in response.json()
    assert db.get_state('browser_model_recovery') == recovery


def test_v2_registration_offers_explicit_scope_once_then_ack_stops_it(selective_bridge):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    expected = {'nonce': recovery['nonce'], 'version': 2, 'providers': ['chatgpt']}
    assert register(selective_bridge, recovery_version=2).json()['recovery_requested'] == expected
    body = recovery_body(recovery)
    acknowledged = client.post('/browser-bridge/window/recovered', json=body, headers=headers)
    assert acknowledged.status_code == 200
    assert acknowledged.json() == {'ok': True, **body}
    completed = db.get_state('browser_model_recovery')
    assert completed['completed'] is True
    assert completed['completed_at'] >= recovery['requested_at']
    assert 'recovery_requested' not in register(selective_bridge, recovery_version=2).json()
    repeated = client.post('/browser-bridge/window/recovered', json=body, headers=headers)
    assert repeated.status_code == 409
    assert db.get_state('browser_model_recovery') == completed


@pytest.mark.parametrize('route', ['retire-for-recovery', 'recovered'])
@pytest.mark.parametrize('change', [
    {'providers': []}, {'providers': ['deepseek']}, {'providers': ['doubao']},
    {'providers': ['glm']}, {'providers': ['chatgpt', 'deepseek']},
    {'providers': ['chatgpt', 'doubao']}, {'providers': ['chatgpt', 'chatgpt']},
    {'providers': 'chatgpt'}, {'providers': None}, {'version': 1}, {'version': '2'},
    {'version': True}, {'nonce': 'a' * 32}, {'window_id': 12}, {'host_tab_id': 23},
    {'window_id': True}, {'host_tab_id': None},
])
def test_invalid_scope_or_identity_rejected_before_native_call(selective_bridge, monkeypatch, route, change):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    window = db.get_state('browser_bridge_window')
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    response = client.post('/browser-bridge/window/' + route,
                           json={**recovery_body(recovery), **change}, headers=headers)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == recovery
    assert db.get_state('browser_bridge_window') == window


@pytest.mark.parametrize('route', ['retire-for-recovery', 'recovered'])
@pytest.mark.parametrize('missing', ['nonce', 'version', 'providers', 'window_id', 'host_tab_id'])
def test_missing_scope_identity_field_rejected(selective_bridge, monkeypatch, route, missing):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    body = recovery_body(recovery)
    del body[missing]
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    response = client.post('/browser-bridge/window/' + route, json=body, headers=headers)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == recovery


@pytest.mark.parametrize('route', ['register', 'retire-for-recovery', 'recovered'])
def test_recovery_bridge_routes_require_pairing_and_object_body(selective_bridge, route):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    before = db.get_state('browser_bridge_window')
    assert client.post('/browser-bridge/window/' + route, json=recovery_body(recovery)).status_code == 403
    assert client.post('/browser-bridge/window/' + route, json=[], headers=headers).status_code == 400
    assert db.get_state('browser_model_recovery') == recovery
    assert db.get_state('browser_bridge_window') == before


def test_v2_retirement_echoes_scope_only_after_exact_verified_retirement(selective_bridge, monkeypatch):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    calls = []

    def retire(database, window_id, host_tab_id, nonce):
        calls.append((database, window_id, host_tab_id, nonce))
        return {'ok': True, 'retired': True, 'visible': True, 'verified': True,
                'manual_reveal': False, 'window_id': window_id, 'host_tab_id': host_tab_id, 'nonce': nonce}

    monkeypatch.setattr('app.browser_window.retire_for_recovery', retire)
    response = client.post('/browser-bridge/window/retire-for-recovery',
                           json=recovery_body(recovery), headers=headers)
    assert response.status_code == 200
    assert response.json()['version'] == 2
    assert response.json()['providers'] == ['chatgpt']
    assert calls == [(db, 11, 22, recovery['nonce'])]
    assert db.get_state('browser_model_recovery') == recovery


@pytest.mark.parametrize('route', ['retire-for-recovery', 'recovered'])
def test_old_nonce_cannot_consume_new_v2_authorization(selective_bridge, monkeypatch, route):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    old = request_recovery(selective_bridge)
    new = request_recovery(selective_bridge)
    assert new['nonce'] != old['nonce']
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    response = client.post('/browser-bridge/window/' + route, json=recovery_body(old), headers=headers)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == new


@pytest.mark.parametrize('route', ['retire-for-recovery', 'recovered'])
@pytest.mark.parametrize('change', ['disabled', 'local_proxy'])
def test_configuration_change_cannot_broaden_frozen_scope(selective_bridge, monkeypatch, route, change):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    if change == 'disabled':
        config.openai.disabled_lanes.append('chatgpt')
    else:
        config.openai.local_proxies['doubao'].enabled = False
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    assert 'recovery_requested' not in register(selective_bridge, recovery_version=2).json()
    response = client.post('/browser-bridge/window/' + route, json=recovery_body(recovery), headers=headers)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == recovery


def test_first_registration_binds_unregistered_request_only_once(selective_bridge):
    config, db, app, client, headers = selective_bridge
    recovery = request_recovery(selective_bridge)
    assert recovery['registration_pending'] is True
    assert recovery['window_id'] is None and recovery['host_tab_id'] is None
    response = register(selective_bridge, recovery_version=2)
    assert response.json()['recovery_requested']['nonce'] == recovery['nonce']
    bound = db.get_state('browser_model_recovery')
    assert bound['registration_pending'] is False
    assert {key: bound[key] for key in IDENTITY} == IDENTITY
    assert client.post('/browser-bridge/window/recovered', json=recovery_body(bound), headers=headers).status_code == 200


@pytest.mark.parametrize('identity', [{'window_id': 12, 'host_tab_id': 22}, {'window_id': 11, 'host_tab_id': 23}])
def test_unproven_registration_replacement_permanently_invalidates_nonce(selective_bridge, identity):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    replaced = register(selective_bridge, identity, recovery_version=2)
    assert 'recovery_requested' not in replaced.json()
    invalidated = db.get_state('browser_model_recovery')
    assert invalidated['invalidated'] is True
    assert invalidated['nonce'] == recovery['nonce']
    assert 'recovery_requested' not in register(selective_bridge, recovery_version=2).json()
    response = client.post('/browser-bridge/window/recovered', json=recovery_body(recovery), headers=headers)
    assert response.status_code == 409
    assert db.get_state('browser_model_recovery') == invalidated


def test_verified_retirement_allows_only_same_host_destination_binding(selective_bridge):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    source = db.get_state('browser_bridge_window')
    source['recovery_retirement'] = {**IDENTITY, 'nonce': recovery['nonce'], 'native': {'proof': 'synthetic'}}
    db.set_state('browser_bridge_window', source)
    destination = {**IDENTITY, 'window_id': 33}
    response = register(selective_bridge, destination, recovery_version=2)
    assert response.json()['recovery_requested'] == {
        'nonce': recovery['nonce'], 'version': 2, 'providers': ['chatgpt']}
    rebound = db.get_state('browser_model_recovery')
    assert {key: rebound[key] for key in IDENTITY} == destination
    assert client.post('/browser-bridge/window/recovered',
                       json=recovery_body(recovery), headers=headers).status_code == 409
    acknowledged = client.post('/browser-bridge/window/recovered',
                               json=recovery_body(rebound, destination), headers=headers)
    assert acknowledged.status_code == 200


@pytest.mark.parametrize('proof_change', [{'nonce': 'a' * 32}, {'window_id': 12}, {'host_tab_id': 23}])
def test_changed_retirement_proof_cannot_transfer_nonce(selective_bridge, proof_change):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    source = db.get_state('browser_bridge_window')
    source['recovery_retirement'] = {**IDENTITY, 'nonce': recovery['nonce'], **proof_change}
    db.set_state('browser_bridge_window', source)
    response = register(selective_bridge, {**IDENTITY, 'window_id': 33}, recovery_version=2)
    assert 'recovery_requested' not in response.json()
    assert db.get_state('browser_model_recovery')['invalidated'] is True


def test_no_eligible_web_provider_rejects_before_resume_or_overwrite(selective_bridge, monkeypatch):
    config, db, app, client, headers = selective_bridge
    config.openai.disabled_lanes.append('chatgpt')
    previous = {'nonce': 'a' * 32, 'completed': False}
    db.set_state('browser_model_recovery', previous)
    calls = []
    monkeypatch.setattr('app.browser_window.resume', lambda database: calls.append(database))
    response = client.post('/browser-window/recover-selected-models',
                           data={'_csrf': app.state.csrf_token}, follow_redirects=False)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == previous
    assert db.get_state('browser_window_feedback') is None


def test_enabled_proxy_requires_selective_protocol_even_without_disabled_lanes(selective_bridge, monkeypatch):
    config, db, app, client, headers = selective_bridge
    config.openai.disabled_lanes.clear()
    register(selective_bridge)
    old = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', old)
    calls = []
    monkeypatch.setattr('app.browser_window.resume', lambda database: calls.append(database))
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    assert 'recovery_requested' not in register(selective_bridge, recovery_version=2).json()
    assert client.post('/browser-window/recover-models',
                       data={'_csrf': app.state.csrf_token}, follow_redirects=False).status_code == 409
    for route in ('retire-for-recovery', 'recovered'):
        response = client.post('/browser-bridge/window/' + route,
                               json={**IDENTITY, 'nonce': old['nonce']}, headers=headers)
        assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == old


@pytest.mark.parametrize('providers', [
    ['doubao', 'chatgpt'], ['chatgpt'], ['chatgpt', 'doubao', 'deepseek'], ['chatgpt', 'doubao', 'doubao'],
])
def test_multi_provider_scope_rejects_reorder_subset_superset_and_duplicates(selective_bridge, monkeypatch, providers):
    config, db, app, client, headers = selective_bridge
    config.openai.local_proxies['doubao'].enabled = False
    register(selective_bridge)
    recovery = request_recovery(selective_bridge)
    assert recovery['providers'] == ['chatgpt', 'doubao']
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    body = {**recovery_body(recovery), 'providers': providers}
    response = client.post('/browser-bridge/window/retire-for-recovery', json=body, headers=headers)
    assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == recovery


def test_legacy_nonce_cannot_be_reinterpreted_as_v2_authorization(selective_bridge, monkeypatch):
    config, db, app, client, headers = selective_bridge
    register(selective_bridge)
    old = {'nonce': 'a' * 32, 'requested_at': 123, 'completed': False}
    db.set_state('browser_model_recovery', old)
    assert 'recovery_requested' not in register(selective_bridge, recovery_version=2).json()
    calls = []
    monkeypatch.setattr('app.browser_window.retire_for_recovery', lambda *args: calls.append(args))
    for route in ('retire-for-recovery', 'recovered'):
        response = client.post('/browser-bridge/window/' + route,
                               json={**IDENTITY, 'nonce': old['nonce'], 'version': 2, 'providers': ['chatgpt']},
                               headers=headers)
        assert response.status_code == 409
    assert calls == []
    assert db.get_state('browser_model_recovery') == old
