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
