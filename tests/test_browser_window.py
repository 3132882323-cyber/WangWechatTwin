from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import psutil
import pytest

from app import browser_window as windows
from app.db import Database


class FakeWindows:
    def __init__(self, title):
        self.windows = {
            101: {"title": title + " - Google Chrome", "class": "Chrome_WidgetWin_1",
                  "pid": 42, "thread_id": 12, "visible": True},
        }
        self.commands = []
        self.before_change = None
        self.after_change = None
        self.ignore_change = False

    def exists(self, hwnd):
        return hwnd in self.windows

    def bound_snapshot(self, hwnd):
        if hwnd not in self.windows:
            raise windows.WindowControlError("owned_window_missing")
        current = self.windows[hwnd]
        if current.get("ancestor", hwnd) != hwnd:
            raise windows.WindowControlError("owned_window_missing")
        if current["class"] != "Chrome_WidgetWin_1":
            raise windows.WindowControlError("owned_window_class_mismatch")
        return self.window_process(hwnd)

    def snapshot(self, hwnd, title):
        window = self.bound_snapshot(hwnd)
        current = self.windows[hwnd]
        if not windows._caption_matches(current["title"], title):
            raise windows.WindowControlError("owned_window_title_mismatch")
        return window

    def window_process(self, hwnd):
        current = self.windows[hwnd]
        return {"hwnd": hwnd, "pid": current["pid"], "thread_id": current["thread_id"]}

    def find(self, title):
        result = []
        for hwnd in self.windows:
            try:
                self.snapshot(hwnd, title)
                result.append(hwnd)
            except windows.WindowControlError:
                pass
        return result

    def visible(self, hwnd):
        if hwnd not in self.windows:
            raise windows.WindowControlError("owned_window_missing")
        return self.windows[hwnd]["visible"]

    def set_visible(self, hwnd, visible):
        if self.before_change:
            self.before_change()
        self.commands.append((hwnd, visible))
        if not self.ignore_change:
            self.windows[hwnd]["visible"] = visible
            if visible:
                self.windows[hwnd]["iconic"] = False
        if self.after_change:
            self.after_change()

    def iconic(self, hwnd):
        return self.windows[hwnd].get("iconic", False)


@pytest.fixture
def registered(tmp_path, monkeypatch):
    db = Database(tmp_path / "window.sqlite3")
    registration = windows.register(db, 7, 9)
    native = FakeWindows(registration["title"])
    identities = {
        42: {"pid": 42, "create_time": 1000.25,
             "exe": r"c:\program files\google\chrome\application\chrome.exe",
             "username": r"computer\owner"},
    }
    monkeypatch.setattr(windows, "_native_api", lambda: native)
    monkeypatch.setattr(windows, "_chrome_identity", lambda pid: dict(identities[pid]))
    monkeypatch.setattr(windows, "_process_start_time", lambda pid: identities.get(pid, {}).get("create_time"))
    return SimpleNamespace(db=db, native=native, identities=identities, title=registration["title"])


def test_registration_is_private_random_and_idempotent(registered):
    first = windows.register(registered.db, 7, 9)
    second = windows.register(registered.db, 7, 9)
    assert first == second
    assert windows._TITLE_RE.fullmatch(first["title"])
    assert first["verified"] is False and first["hidden"] is False
    assert "native" not in first and "hwnd" not in first
    assert registered.native.commands == []


@pytest.mark.parametrize("window_id,tab_id", [(-1, 2), (1, -2), (True, 2), (1, False), ("1", 2), (1, None)])
def test_registration_rejects_non_native_ids(tmp_path, window_id, tab_id):
    db = Database(tmp_path / "invalid.sqlite3")
    assert windows.register(db, window_id, tab_id)["error"] == "invalid_window_registration"
    assert db.get_state(windows.STATE_KEY) is None


def test_hiding_persists_identity_first_and_verifies_native_visibility(registered):
    def verify_before_mutation():
        saved = registered.db.get_state(windows.STATE_KEY)
        assert saved["native"]["hwnd"] == 101
        assert saved["native"]["create_time"] == 1000.25
        assert saved["verified"] is False

    registered.native.before_change = verify_before_mutation
    result = windows.hide(registered.db)
    assert result == {"ok": True, "hidden": True, "visible": False, "verified": True,
                      "window_id": 7, "host_tab_id": 9, "title": registered.title, "manual_reveal": False}
    assert registered.native.commands == [(101, False)]
    saved = registered.db.get_state(windows.STATE_KEY)
    assert saved["hidden"] is True and saved["verified"] is True
    assert "hwnd" not in result and "native" not in result and "exe" not in result


@pytest.mark.parametrize("suffix", [" extra", " - Google Chrome - personal", " - Chromium", "0"])
def test_nearby_or_unrelated_title_is_never_hidden(registered, suffix):
    registered.native.windows[101]["title"] = registered.title + suffix
    result = windows.hide(registered.db)
    assert result["error"] == "owned_window_missing"
    assert result["hidden"] is False
    assert registered.native.commands == []


def test_a_chrome_title_on_another_window_class_is_never_hidden(registered):
    registered.native.windows[101]["class"] = "Notepad"
    assert windows.hide(registered.db)["error"] == "owned_window_missing"
    assert registered.native.commands == []


def test_duplicate_title_fails_closed_without_hiding_either_window(registered):
    registered.native.windows[102] = dict(registered.native.windows[101])
    assert windows.hide(registered.db)["error"] == "owned_window_ambiguous"
    assert registered.native.commands == []


def test_failed_showwindow_never_reports_hidden(registered):
    registered.native.ignore_change = True
    result = windows.hide(registered.db)
    assert result["error"] == "window_visibility_not_confirmed"
    assert result["hidden"] is False and result["verified"] is False
    assert registered.db.get_state(windows.STATE_KEY)["hidden"] is False


def test_restore_requires_a_previously_bound_window(registered):
    result = windows.show(registered.db)
    assert result["error"] == "window_not_bound"
    assert registered.native.commands == []


def test_restore_rechecks_and_shows_only_original_window(registered):
    assert windows.hide(registered.db)["hidden"] is True
    result = windows.show(registered.db)
    assert result["ok"] is True and result["visible"] is True
    assert result["hidden"] is False and result["verified"] is True
    assert result["manual_reveal"] is True
    assert registered.native.commands == [(101, False), (101, True)]


def test_restore_allows_active_model_title_only_for_the_previously_bound_window(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[102] = dict(registered.native.windows[101])
    registered.native.windows[101]["title"] = "Model response - Google Chrome"

    result = windows.show(registered.db)

    assert result["ok"] is True and result["verified"] is True
    assert result["visible"] is True and result["manual_reveal"] is True
    assert registered.native.windows[102]["visible"] is False
    assert registered.native.commands == [(101, False), (101, True)]


def test_hiding_still_requires_the_private_host_title_after_binding(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[101]["title"] = "Model response - Google Chrome"

    result = windows.hide(registered.db)

    assert result["error"] == "owned_window_title_mismatch"
    assert result["verified"] is False
    assert registered.native.commands == [(101, False)]


def test_manual_reveal_blocks_automatic_hide_until_explicit_resume(registered, monkeypatch):
    assert windows.hide(registered.db)["hidden"] is True
    assert windows.show(registered.db)["manual_reveal"] is True
    assert windows.register(registered.db, 7, 9)["manual_reveal"] is True
    with monkeypatch.context() as context:
        context.setattr(windows, "_native_api", lambda: pytest.fail("manual reveal must not touch native window"))
        result = windows.hide(registered.db)
        assert result["error"] == "background_window_manual_reveal"
        assert result["hidden"] is False and result["verified"] is False
        native_before = registered.db.get_state(windows.STATE_KEY)["native"]
        assert windows.resume(registered.db)["manual_reveal"] is False
        assert registered.db.get_state(windows.STATE_KEY)["native"] == native_before
    assert registered.native.commands == [(101, False), (101, True)]
    assert windows.hide(registered.db)["hidden"] is True


def test_failed_restore_does_not_start_manual_reveal(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.ignore_change = True
    assert windows.show(registered.db)["error"] == "window_visibility_not_confirmed"
    assert registered.db.get_state(windows.STATE_KEY)["manual_reveal"] is False


def test_restore_returns_minimized_window_to_visible_normal_state(registered):
    registered.native.windows[101]["iconic"] = True
    assert windows.hide(registered.db)["hidden"] is True
    result = windows.show(registered.db)
    assert result["ok"] is True and result["verified"] is True
    assert registered.native.iconic(101) is False


def test_restore_does_not_claim_success_if_window_stays_minimized(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[101].update(visible=True, iconic=True)
    registered.native.ignore_change = True
    result = windows.show(registered.db)
    assert result["error"] == "window_visibility_not_confirmed"
    assert result["verified"] is False and result["manual_reveal"] is False


@pytest.mark.parametrize("changed", ["create_time", "exe", "username", "pid", "thread_id", "class", "ancestor"])
def test_restore_rejects_recycled_process_or_window_identity(registered, changed):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[101]["title"] = "Model response - Google Chrome"
    if changed == "thread_id":
        registered.native.windows[101][changed] += 1
    elif changed == "pid":
        registered.native.windows[101][changed] = 43
        registered.identities[43] = {**registered.identities[42], "pid": 43}
    elif changed == "class":
        registered.native.windows[101][changed] = "Notepad"
    elif changed == "ancestor":
        registered.native.windows[101][changed] = 102
    elif changed == "create_time":
        registered.identities[42][changed] += 10
    else:
        registered.identities[42][changed] = "different"
    result = windows.show(registered.db)
    assert result["ok"] is False and result["verified"] is False
    assert registered.native.commands == [(101, False)]


def test_restore_rechecks_process_identity_after_native_show(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[101]["title"] = "Model response - Google Chrome"
    registered.native.after_change = lambda: registered.identities[42].update(create_time=2000.25)

    result = windows.show(registered.db)

    assert result["error"] == "owned_window_identity_changed"
    assert result["verified"] is False and result["manual_reveal"] is False
    assert registered.native.commands == [(101, False), (101, True)]


def test_bound_handle_never_falls_back_to_another_matching_window(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[102] = registered.native.windows.pop(101)
    result = windows.hide(registered.db)
    assert result["error"] == "owned_window_missing"
    assert registered.native.commands == [(101, False)]


def test_disappearing_window_does_not_turn_false_visibility_into_success(registered):
    registered.native.after_change = lambda: registered.native.windows.pop(101)
    result = windows.hide(registered.db)
    assert result["error"] == "owned_window_missing"
    assert result["hidden"] is False and result["verified"] is False


def test_registration_cannot_orphan_an_existing_hidden_window(registered):
    assert windows.hide(registered.db)["hidden"] is True
    result = windows.register(registered.db, 8, 10)
    assert result["error"] == "owned_window_already_hidden"
    assert registered.db.get_state(windows.STATE_KEY)["title"] == registered.title
    assert windows.show(registered.db)["visible"] is True


def test_closed_original_window_allows_fresh_registration(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows.clear()
    result = windows.register(registered.db, 8, 10)
    assert result["ok"] is True and result["title"] != registered.title
    assert registered.db.get_state(windows.STATE_KEY)["native"] is None


@pytest.mark.parametrize("reused_handle", [False, True])
def test_browser_restart_same_extension_ids_can_bind_only_after_reregistration(registered, reused_handle):
    assert windows.hide(registered.db)["hidden"] is True
    if reused_handle:
        registered.identities[42]["create_time"] += 100
    else:
        registered.native.windows[102] = registered.native.windows.pop(101)
    assert windows.hide(registered.db)["ok"] is False
    registered.native.commands.clear()
    result = windows.register(registered.db, 7, 9)
    assert result["ok"] is True and result["title"] == registered.title
    assert registered.db.get_state(windows.STATE_KEY)["native"] is None
    assert windows.hide(registered.db)["hidden"] is True


def test_registration_cannot_orphan_hidden_window_after_active_tab_changes(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.native.windows[101]["title"] = "Login page - Google Chrome"
    assert windows.register(registered.db, 8, 10)["error"] == "owned_window_already_hidden"
    assert registered.db.get_state(windows.STATE_KEY)["title"] == registered.title


def test_corrupted_registration_never_accesses_native_api(tmp_path, monkeypatch):
    db = Database(tmp_path / "corrupt.sqlite3")
    db.set_state(windows.STATE_KEY, {"window_id": 1, "host_tab_id": 2, "title": "Google Chrome"})
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("unregistered desktop access"))
    assert windows.hide(db)["error"] == "window_not_registered"
    assert windows.show(db)["error"] == "window_not_registered"


def test_unsupported_platform_returns_unverified_failure(registered, monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "linux")
    monkeypatch.setattr(windows, "_native_api", windows._Windows)
    result = windows.hide(registered.db)
    assert result["error"] == "windows_required"
    assert result["hidden"] is False and result["verified"] is False


@pytest.mark.parametrize("failure", [psutil.AccessDenied(42), psutil.NoSuchProcess(42)])
def test_inaccessible_process_fails_without_hiding(registered, monkeypatch, failure):
    def reject(_):
        raise failure

    monkeypatch.setattr(windows, "_chrome_identity", reject)
    assert windows.hide(registered.db)["error"] == "native_access_failed"
    assert registered.native.commands == []


@pytest.mark.parametrize(
    "executable,username,allowed,error",
    [
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"PC\owner", True, None),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"PC\other", True, "chrome_user_mismatch"),
        (r"C:\Temp\chrome.exe", r"PC\owner", True, "chrome_executable_not_verified"),
        (r"C:\Portable\Google\Chrome\Application\chrome.exe", r"PC\owner", False, "chrome_executable_not_verified"),
    ],
)
def test_process_checks_installed_executable_and_windows_owner(monkeypatch, executable, username, allowed, error):
    fake_process = SimpleNamespace(
        oneshot=lambda: nullcontext(), exe=lambda: executable,
        username=lambda: username, create_time=lambda: 1234.5,
    )
    owner = SimpleNamespace(username=lambda: r"PC\owner")
    monkeypatch.setattr(windows.psutil, "Process", lambda pid: fake_process if pid == 42 else owner)
    path = windows._path_key(windows.os.path.realpath(executable))
    monkeypatch.setattr(windows, "_installed_chrome_paths", lambda: {path} if allowed else set())
    if error:
        with pytest.raises(windows.WindowControlError, match=error):
            windows._chrome_identity(42)
    else:
        assert windows._chrome_identity(42)["create_time"] == 1234.5


def test_native_adapter_uses_hide_not_minimize_and_separately_reads_visibility():
    calls = []
    adapter = windows._Windows.__new__(windows._Windows)
    adapter.api = SimpleNamespace(
        ShowWindow=lambda handle, command: calls.append((handle, command)),
        IsWindow=lambda _: True,
        IsWindowVisible=lambda _: False,
        IsIconic=lambda _: False,
    )
    adapter.set_visible(101, False)
    assert calls == [(101, 0)]
    assert adapter.visible(101) is False
    adapter.set_visible(101, True)
    assert calls[-1] == (101, 9)
    assert adapter.iconic(101) is False
