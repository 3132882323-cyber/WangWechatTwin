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
    # These cases exercise the Win32 adapter contract on every CI host.
    monkeypatch.setattr(windows.sys, "platform", "win32")
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


@pytest.fixture
def recovery_window(registered):
    assert windows.hide(registered.db)["hidden"] is True
    registered.recovery = {"nonce": "a" * 32, "requested_at": 123, "completed": False}
    registered.db.set_state("browser_model_recovery", registered.recovery)
    registered.db.set_state("unrelated_bridge_state", {"preserve": True})
    return registered


@pytest.mark.parametrize(
    "window_id,tab_id,error",
    [(-1, 9, "invalid_window_registration"), (True, 9, "invalid_window_registration"),
     (7, "9", "invalid_window_registration"), (7, None, "invalid_window_registration"),
     (8, 9, "recovery_registration_mismatch"), (7, 10, "recovery_registration_mismatch")],
)
def test_recovery_retirement_requires_exact_registered_ids(recovery_window, monkeypatch, window_id, tab_id, error):
    db = recovery_window.db
    saved = db.get_state(windows.STATE_KEY)
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("invalid recovery accessed native windows"))
    result = windows.retire_for_recovery(db, window_id, tab_id, recovery_window.recovery["nonce"])
    assert result["error"] == error
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == recovery_window.recovery


@pytest.mark.parametrize("case", ["missing", "foreign", "completed", "malformed", "non_string"])
def test_recovery_retirement_requires_current_pending_nonce(recovery_window, monkeypatch, case):
    db = recovery_window.db
    saved = db.get_state(windows.STATE_KEY)
    nonce = recovery_window.recovery["nonce"]
    if case == "missing":
        db.set_state("browser_model_recovery", {})
    elif case == "foreign":
        nonce = "b" * 32
    elif case == "completed":
        db.set_state("browser_model_recovery", {**recovery_window.recovery, "completed": True})
    elif case == "malformed":
        nonce = "bad-nonce"
        db.set_state("browser_model_recovery", {**recovery_window.recovery, "nonce": nonce})
    else:
        nonce = ["a" * 32]
    recovery_before = db.get_state("browser_model_recovery")
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("invalid nonce accessed native windows"))
    assert windows.retire_for_recovery(db, 7, 9, nonce)["error"] == "recovery_nonce_mismatch"
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == recovery_before


def test_recovery_retirement_respects_manual_reveal(recovery_window, monkeypatch):
    db = recovery_window.db
    state = db.get_state(windows.STATE_KEY)
    state["manual_reveal"] = True
    db.set_state(windows.STATE_KEY, state)
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("manual reveal accessed native windows"))
    result = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    assert result["error"] == "background_window_manual_reveal"
    assert db.get_state(windows.STATE_KEY) == state


def test_recovery_restores_mixed_window_before_releasing_only_its_binding(recovery_window):
    db, native = recovery_window.db, recovery_window.native
    state = db.get_state(windows.STATE_KEY)
    state["preserved_field"] = "keep"
    db.set_state(windows.STATE_KEY, state)
    native.windows[101].update(title="Personal tab - Google Chrome", iconic=True)
    native.windows[102] = dict(native.windows[101])

    def require_saved_binding():
        assert db.get_state(windows.STATE_KEY)["native"] == state["native"]

    native.before_change = require_saved_binding
    result = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])

    assert result == {
        "ok": True, "retired": True, "visible": True, "verified": True, "hidden": False,
        "manual_reveal": False, "window_id": 7, "host_tab_id": 9, "title": state["title"],
        "nonce": recovery_window.recovery["nonce"], "code": "window_retired_for_recovery",
    }
    assert native.commands == [(101, False), (101, True)]
    assert native.visible(101) is True and native.iconic(101) is False
    assert native.visible(102) is False
    saved = db.get_state(windows.STATE_KEY)
    assert saved["native"] is None and saved["verified"] is False
    assert saved["preserved_field"] == "keep" and saved["manual_reveal"] is False
    assert saved["recovery_retirement"]["native"] == state["native"]
    assert db.get_state("browser_model_recovery") == recovery_window.recovery
    assert db.get_state("unrelated_bridge_state") == {"preserve": True}


@pytest.mark.parametrize("changed", ["create_time", "exe", "username", "pid", "thread_id", "class", "ancestor"])
def test_recovery_retirement_rejects_changed_native_identity(recovery_window, changed):
    native = recovery_window.native
    saved = recovery_window.db.get_state(windows.STATE_KEY)
    if changed == "thread_id":
        native.windows[101][changed] += 1
    elif changed == "pid":
        native.windows[101][changed] = 43
        recovery_window.identities[43] = {**recovery_window.identities[42], "pid": 43}
    elif changed == "class":
        native.windows[101][changed] = "Notepad"
    elif changed == "ancestor":
        native.windows[101][changed] = 102
    elif changed == "create_time":
        recovery_window.identities[42][changed] += 10
    else:
        recovery_window.identities[42][changed] = "different"
    result = windows.retire_for_recovery(recovery_window.db, 7, 9, recovery_window.recovery["nonce"])
    assert result["ok"] is False and result["verified"] is False
    assert native.commands == [(101, False)]
    assert recovery_window.db.get_state(windows.STATE_KEY) == saved


@pytest.mark.parametrize("case,error", [
    ("hidden", "window_visibility_not_confirmed"), ("minimized", "window_visibility_not_confirmed"),
    ("identity_changed", "owned_window_identity_changed"), ("disappeared", "owned_window_missing"),
])
def test_recovery_retirement_preserves_binding_if_restore_is_unverified(recovery_window, case, error):
    db, native = recovery_window.db, recovery_window.native
    saved = db.get_state(windows.STATE_KEY)
    if case in {"hidden", "minimized"}:
        native.ignore_change = True
        if case == "minimized":
            native.windows[101].update(visible=True, iconic=True)
    elif case == "identity_changed":
        native.after_change = lambda: recovery_window.identities[42].update(create_time=2000.25)
    else:
        native.after_change = lambda: native.windows.pop(101)
    result = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    assert result["error"] == error and result["verified"] is False
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == recovery_window.recovery


def test_recovery_retirement_rechecks_nonce_after_restore(recovery_window):
    db = recovery_window.db
    saved = db.get_state(windows.STATE_KEY)
    replacement = {**recovery_window.recovery, "nonce": "b" * 32}
    recovery_window.native.after_change = lambda: db.set_state("browser_model_recovery", replacement)
    result = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    assert result["error"] == "recovery_nonce_mismatch"
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == replacement


def test_recovery_retirement_retry_verifies_saved_proof_without_another_restore(recovery_window):
    db = recovery_window.db
    first = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    saved = db.get_state(windows.STATE_KEY)
    second = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    assert first == second and second["retired"] is True
    assert recovery_window.native.commands == [(101, False), (101, True)]
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == recovery_window.recovery


@pytest.mark.parametrize("change,error", [
    ("hidden", "window_visibility_not_confirmed"), ("minimized", "window_visibility_not_confirmed"),
    ("identity", "owned_window_identity_changed"), ("foreign_proof", "window_not_bound"),
])
def test_recovery_retirement_retry_does_not_trust_stale_visibility_proof(recovery_window, change, error):
    db = recovery_window.db
    assert windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])["retired"] is True
    if change == "hidden":
        recovery_window.native.windows[101]["visible"] = False
    elif change == "minimized":
        recovery_window.native.windows[101]["iconic"] = True
    elif change == "identity":
        recovery_window.identities[42]["create_time"] += 100
    else:
        state = db.get_state(windows.STATE_KEY)
        state["recovery_retirement"]["nonce"] = "b" * 32
        db.set_state(windows.STATE_KEY, state)
    saved = db.get_state(windows.STATE_KEY)
    result = windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])
    assert result["error"] == error and result["verified"] is False
    assert recovery_window.native.commands == [(101, False), (101, True)]
    assert db.get_state(windows.STATE_KEY) == saved


def test_recovery_retirement_never_claims_visibility_for_unbound_registration(registered, monkeypatch):
    registered.db.set_state("browser_model_recovery", {"nonce": "a" * 32, "completed": False})
    state = registered.db.get_state(windows.STATE_KEY)
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("unbound recovery accessed native windows"))
    result = windows.retire_for_recovery(registered.db, 7, 9, "a" * 32)
    assert result["error"] == "window_not_bound" and result["verified"] is False
    assert registered.db.get_state(windows.STATE_KEY) == state


def test_new_window_registration_is_accepted_after_verified_retirement(recovery_window, monkeypatch):
    db = recovery_window.db
    assert windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])["retired"] is True
    with monkeypatch.context() as context:
        context.setattr(windows, "_native_api", lambda: pytest.fail("new registration must not change the retired window"))
        result = windows.register(db, 8, 9)
    assert result["ok"] is True and result["window_id"] == 8 and result["host_tab_id"] == 9
    assert db.get_state(windows.STATE_KEY)["native"] is None
    assert "recovery_retirement" not in db.get_state(windows.STATE_KEY)
    assert db.get_state("browser_model_recovery") == recovery_window.recovery
    assert recovery_window.native.visible(101) is True
    assert windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])["error"] == "recovery_registration_mismatch"


def test_late_hide_cannot_rebind_and_hide_a_retired_mixed_window(recovery_window, monkeypatch):
    db, native = recovery_window.db, recovery_window.native
    assert windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])["retired"] is True
    saved = db.get_state(windows.STATE_KEY)
    # The private host title is still present until the extension moves it. A
    # delayed hide used to rediscover this exact window and hide personal tabs.
    assert native.snapshot(101, saved["title"])["hwnd"] == 101
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("retired hide must not inspect native windows"))
    result = windows.hide(db)
    assert result["error"] == "background_window_retired_for_recovery"
    assert result["ok"] is False and result["hidden"] is False
    assert db.get_state(windows.STATE_KEY) == saved
    assert native.commands == [(101, False), (101, True)]
    assert native.visible(101) is True


@pytest.mark.parametrize("case", ["completed", "new_nonce_old_request", "new_nonce_new_request", "missing"])
def test_old_registration_stays_retired_across_recovery_state_changes(recovery_window, monkeypatch, case):
    db, native = recovery_window.db, recovery_window.native
    nonce = recovery_window.recovery["nonce"]
    assert windows.retire_for_recovery(db, 7, 9, nonce)["retired"] is True
    saved = db.get_state(windows.STATE_KEY)
    if case == "completed":
        db.set_state("browser_model_recovery", {**recovery_window.recovery, "completed": True})
    elif case.startswith("new_nonce"):
        db.set_state("browser_model_recovery", {**recovery_window.recovery, "nonce": "b" * 32})
        if case == "new_nonce_new_request":
            nonce = "b" * 32
    else:
        db.set_state("browser_model_recovery", {})
    recovery_before = db.get_state("browser_model_recovery")
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("old registration must remain retired"))
    assert windows.register(db, 7, 9)["ok"] is True
    assert windows.resume(db)["ok"] is True
    assert windows.retire_for_recovery(db, 7, 9, nonce)["ok"] is False
    assert windows.hide(db)["error"] == "background_window_retired_for_recovery"
    assert db.get_state(windows.STATE_KEY) == saved
    assert db.get_state("browser_model_recovery") == recovery_before
    assert native.commands == [(101, False), (101, True)]
    assert native.visible(101) is True


@pytest.mark.parametrize("proof", [None, {}, False])
def test_presence_of_corrupt_retirement_proof_still_blocks_hide(registered, monkeypatch, proof):
    state = registered.db.get_state(windows.STATE_KEY)
    state["recovery_retirement"] = proof
    registered.db.set_state(windows.STATE_KEY, state)
    monkeypatch.setattr(windows, "_native_api", lambda: pytest.fail("corrupt retirement proof must fail closed"))
    assert windows.hide(registered.db)["error"] == "background_window_retired_for_recovery"
    assert registered.db.get_state(windows.STATE_KEY) == state
    assert registered.native.commands == []


def test_new_registration_can_hide_only_new_window_after_retirement(recovery_window):
    db, native = recovery_window.db, recovery_window.native
    assert windows.retire_for_recovery(db, 7, 9, recovery_window.recovery["nonce"])["retired"] is True
    registered = windows.register(db, 8, 9)
    native.windows[102] = {**native.windows[101], "title": registered["title"] + " - Google Chrome"}
    assert windows.hide(db)["hidden"] is True
    assert db.get_state(windows.STATE_KEY)["native"]["hwnd"] == 102
    assert "recovery_retirement" not in db.get_state(windows.STATE_KEY)
    assert native.commands == [(101, False), (101, True), (102, False)]
    assert native.visible(101) is True and native.visible(102) is False
