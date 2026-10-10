from __future__ import annotations

import pytest

from app import browser_window as windows
from app.db import Database


@pytest.fixture
def mac_window(tmp_path, monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "darwin")
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("macOS accessed Win32"))
    monkeypatch.setattr(windows, "_chrome_identity", lambda _: pytest.fail("macOS inspected processes"))
    db = Database(tmp_path / "mac-window.sqlite3")
    registration = windows.register(db, 7, 9)
    assert registration["ok"] is True
    return db, registration["title"]


@pytest.mark.parametrize("platform,mode,hidden,minimized", [
    ("win32", "native_hidden", True, False),
    ("darwin", "extension_minimized", False, True),
    ("linux", "unsupported", False, False),
])
def test_capabilities_report_implementation_not_a_runtime_success(monkeypatch, platform, mode, hidden, minimized):
    monkeypatch.setattr(windows.sys, "platform", platform)
    result = windows.capabilities()
    assert result["mode"] == mode
    assert result["native_hide"] is hidden
    assert result["fully_hidden"] is hidden
    assert result["extension_minimize"] is minimized
    assert "verified" not in result


def test_mac_hide_only_requests_minimization_until_extension_reports(mac_window):
    db, title = mac_window
    requested = windows.hide(db)
    assert requested["ok"] is True and requested["action"] == "minimize"
    assert requested["hidden"] is False and requested["minimized"] is False
    assert requested["visible"] is None and requested["verified"] is False
    assert db.get_state(windows.STATE_KEY)["native"] is None

    report = windows.report_extension_visibility(db, 7, 9, title, "minimized")
    assert report["ok"] is True and report["verified"] is True
    assert report["window_mode"] == "extension_minimized"
    assert report["verification_source"] == "chrome_extension"
    assert report["minimized"] is True and report["visible"] is False
    assert report["hidden"] is False
    saved = db.get_state(windows.STATE_KEY)
    assert saved["minimized"] is True and saved["hidden"] is False
    assert saved["verification_source"] == "chrome_extension"


def test_mac_manual_reveal_waits_for_extension_and_rejects_late_minimize(mac_window):
    db, title = mac_window
    windows.report_extension_visibility(db, 7, 9, title, "minimized")
    requested = windows.show(db)
    assert requested["ok"] is True and requested["action"] == "restore"
    assert requested["manual_reveal"] is True and requested["verified"] is False
    assert requested["visible"] is None
    assert windows.hide(db)["error"] == "background_window_manual_reveal"
    saved = db.get_state(windows.STATE_KEY)
    assert windows.report_extension_visibility(db, 7, 9, title, "minimized")["error"] == "background_window_manual_reveal"
    assert db.get_state(windows.STATE_KEY) == saved
    shown = windows.report_extension_visibility(db, 7, 9, title, "normal")
    assert shown["visible"] is True and shown["verified"] is True
    assert shown["hidden"] is False and shown["minimized"] is False

    windows.resume(db)
    assert windows.report_extension_visibility(db, 7, 9, title, "normal")["error"] == "extension_window_visibility_stale"
    assert windows.hide(db)["action"] == "minimize"
    assert windows.report_extension_visibility(db, 7, 9, title, "minimized")["minimized"] is True


@pytest.mark.parametrize("window_id,tab_id,title_change,state,error", [
    (True, 9, False, "minimized", "extension_window_registration_mismatch"),
    (7, True, False, "minimized", "extension_window_registration_mismatch"),
    (8, 9, False, "minimized", "extension_window_registration_mismatch"),
    (7, 10, False, "minimized", "extension_window_registration_mismatch"),
    (7, 9, True, "minimized", "extension_window_registration_mismatch"),
    (7, 9, False, "hidden", "extension_window_state_invalid"),
    (7, 9, False, None, "extension_window_state_invalid"),
    (7, 9, False, {}, "extension_window_state_invalid"),
    (7, 9, False, "normal", "extension_window_visibility_stale"),
])
def test_mac_rejects_foreign_or_invalid_visibility_without_state_changes(mac_window, window_id, tab_id, title_change, state, error):
    db, title = mac_window
    saved = db.get_state(windows.STATE_KEY)
    result = windows.report_extension_visibility(db, window_id, tab_id, title + "x" if title_change else title, state)
    assert result["error"] == error and result["verified"] is False
    assert db.get_state(windows.STATE_KEY) == saved


def test_mac_does_not_reinterpret_copied_windows_native_identity(mac_window):
    db, title = mac_window
    saved = db.get_state(windows.STATE_KEY)
    saved["native"] = {"hwnd": 123}
    db.set_state(windows.STATE_KEY, saved)
    for result in (windows.register(db, 7, 9), windows.register(db, 8, 10), windows.hide(db),
                   windows.show(db), windows.report_extension_visibility(db, 7, 9, title, "minimized")):
        assert result["error"] == "native_state_platform_mismatch"
        assert result["hidden"] is False and result["verified"] is False
    assert db.get_state(windows.STATE_KEY) == saved


def test_mac_mixed_window_recovery_preserves_original_window_and_registration(mac_window):
    db, _ = mac_window
    nonce = "a" * 32
    db.set_state("browser_model_recovery", {"nonce": nonce, "completed": False})
    saved = db.get_state(windows.STATE_KEY)
    result = windows.retire_for_recovery(db, 7, 9, nonce)
    assert result["error"] == "background_window_mixed_tabs"
    assert result["verified"] is False
    assert db.get_state(windows.STATE_KEY) == saved


def test_retired_mac_registration_cannot_report_background_ready(mac_window):
    db, title = mac_window
    saved = db.get_state(windows.STATE_KEY)
    saved["recovery_retirement"] = {}
    db.set_state(windows.STATE_KEY, saved)
    assert windows.report_extension_visibility(db, 7, 9, title, "minimized")["error"] == "background_window_retired_for_recovery"
    assert db.get_state(windows.STATE_KEY) == saved


def test_extension_report_can_never_substitute_for_native_windows_proof(mac_window, monkeypatch):
    db, title = mac_window
    monkeypatch.setattr(windows.sys, "platform", "win32")
    saved = db.get_state(windows.STATE_KEY)
    result = windows.report_extension_visibility(db, 7, 9, title, "minimized")
    assert result["error"] == "extension_window_visibility_unsupported"
    assert db.get_state(windows.STATE_KEY) == saved


def test_mac_registration_and_minimization_do_not_require_psutil(mac_window, monkeypatch):
    db, title = mac_window
    monkeypatch.setattr(windows, "psutil", None)
    assert windows.register(db, 7, 9)["ok"] is True
    assert windows.hide(db)["action"] == "minimize"
    assert windows.report_extension_visibility(db, 7, 9, title, "minimized")["minimized"] is True


def test_mac_routes_require_pairing_and_show_only_confirmed_minimization(mac_window, tmp_path):
    from fastapi.testclient import TestClient
    from app.config import AppConfig
    from app.webui import create_app

    db, title = mac_window
    config = AppConfig(project_root=tmp_path)
    config.openai.provider = "hybrid_web"
    app = create_app(config, db)
    client = TestClient(app)
    token = (config.resolve(config.paths.browser_bridge) / "pairing_token.txt").read_text().strip()
    headers = {"authorization": "Bearer " + token}
    identity = {"window_id": 7, "host_tab_id": 9, "title": title}

    registration = client.post("/browser-bridge/window/register", json=identity, headers=headers).json()
    assert registration["capabilities"]["mode"] == "extension_minimized"
    assert registration["capabilities"]["fully_hidden"] is False
    minimized = {**identity, "state": "minimized"}
    assert client.post("/browser-bridge/window/visibility", json=minimized).status_code == 403
    assert client.post("/browser-bridge/window/visibility", json=[], headers=headers).status_code == 400
    assert db.get_state(windows.STATE_KEY)["verified"] is False
    wrong = client.post("/browser-bridge/window/visibility", json={**minimized, "window_id": 8}, headers=headers).json()
    assert wrong["ok"] is False
    assert db.get_state(windows.STATE_KEY)["verified"] is False

    instruction = client.post("/browser-bridge/window/hide", json=identity, headers=headers).json()
    assert instruction["action"] == "minimize" and instruction["verified"] is False
    assert "已最小化（Mac，未完全隐藏）" not in client.get("/").text
    receipt = client.post("/browser-bridge/window/visibility", json=minimized, headers=headers).json()
    assert receipt["minimized"] is True and receipt["hidden"] is False
    assert "已最小化（Mac，未完全隐藏）" in client.get("/").text
    assert "已真正隐藏" not in client.get("/").text

    assert client.post("/browser-window/show").status_code == 403
    response = client.post("/browser-window/show", data={"_csrf": app.state.csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get_state(windows.STATE_KEY)["manual_reveal"] is True
    assert db.get_state(windows.STATE_KEY)["verified"] is False
    assert "已显示" not in db.get_state("browser_window_feedback")["message"]
    shown = client.post("/browser-bridge/window/visibility", json={**identity, "state": "normal"}, headers=headers).json()
    assert shown["visible"] is True and shown["minimized"] is False
