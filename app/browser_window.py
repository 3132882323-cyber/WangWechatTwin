"""Hide only the Chrome window registered by the authenticated browser bridge.

The extension keeps its private host tab active while calling ``hide``.  The
native handle and process identity remain local; an HTTP caller never supplies
a HWND.  Importing this module does not inspect or change desktop windows.
"""
from __future__ import annotations

import ctypes
import math
import ntpath
import os
import re
import secrets
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

import psutil


STATE_KEY = "browser_bridge_window"
TITLE_PREFIX = "WeChatTwin Background "
_TITLE_RE = re.compile(re.escape(TITLE_PREFIX) + r"[0-9a-f]{32}\Z")
_CHROME_CLASS = "Chrome_WidgetWin_1"
_CHROME_SUFFIX = r"\google\chrome\application\chrome.exe"
_WINDOW_LOCK = threading.RLock()
SW_HIDE = 0
SW_RESTORE = 9


class WindowControlError(RuntimeError):
    """A stable, non-sensitive error code safe to return to the bridge."""


def _identifier(value: Any) -> bool:
    return type(value) is int and value >= 0


def _valid_state(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _identifier(value.get("window_id"))
        and _identifier(value.get("host_tab_id"))
        and isinstance(value.get("title"), str)
        and _TITLE_RE.fullmatch(value["title"]) is not None
    )


def _caption_matches(caption: str, title: str) -> bool:
    return caption in (title, title + " - Google Chrome")


def _path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _installed_chrome_paths() -> set[str]:
    """Allow installed Google Chrome, never an arbitrary chrome.exe basename."""
    candidates = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(ntpath.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    if sys.platform == "win32":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(
                        hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                        0, winreg.KEY_READ | view,
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        if isinstance(value, str):
                            candidates.append(os.path.expandvars(value).strip('"'))
                except OSError:
                    continue
    return {
        _path_key(os.path.realpath(value))
        for value in candidates
        if ntpath.isabs(value)
        and _path_key(value).endswith(_CHROME_SUFFIX)
        and os.path.isfile(value)
    }


def _chrome_identity(pid: int) -> dict[str, Any]:
    process = psutil.Process(pid)
    with process.oneshot():
        executable = _path_key(os.path.realpath(process.exe()))
        username = process.username()
        created = process.create_time()
    if (
        ntpath.basename(executable) != "chrome.exe"
        or not executable.endswith(_CHROME_SUFFIX)
        or executable not in _installed_chrome_paths()
    ):
        raise WindowControlError("chrome_executable_not_verified")
    owner = psutil.Process(os.getpid()).username()
    if not username or username.casefold() != owner.casefold():
        raise WindowControlError("chrome_user_mismatch")
    if not math.isfinite(created) or created <= 0:
        raise WindowControlError("chrome_process_identity_unavailable")
    return {"pid": pid, "create_time": created, "exe": executable, "username": username.casefold()}


def _process_start_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


class _Windows:
    """Small Win32 adapter; desktop access occurs only inside explicit calls."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowControlError("windows_required")
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "EnumWindows": ([self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result

    def exists(self, hwnd: int) -> bool:
        return bool(self.api.IsWindow(hwnd))

    def bound_snapshot(self, hwnd: int) -> dict[str, int]:
        """Recheck a saved top-level Chrome handle without relying on its tab title."""
        if not self.exists(hwnd) or self.api.GetAncestor(hwnd, 2) != hwnd:
            raise WindowControlError("owned_window_missing")
        window_class = ctypes.create_unicode_buffer(128)
        self.api.GetClassNameW(hwnd, window_class, len(window_class))
        if window_class.value != _CHROME_CLASS:
            raise WindowControlError("owned_window_class_mismatch")
        return self.window_process(hwnd)

    def snapshot(self, hwnd: int, title: str) -> dict[str, int]:
        window = self.bound_snapshot(hwnd)
        caption = ctypes.create_unicode_buffer(256)
        self.api.GetWindowTextW(hwnd, caption, len(caption))
        if not _caption_matches(caption.value, title):
            raise WindowControlError("owned_window_title_mismatch")
        return window

    def window_process(self, hwnd: int) -> dict[str, int]:
        if not self.exists(hwnd):
            raise WindowControlError("owned_window_missing")
        pid = wintypes.DWORD()
        thread_id = int(self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
        if not pid.value or not thread_id:
            raise WindowControlError("owned_window_process_unavailable")
        return {"hwnd": hwnd, "pid": int(pid.value), "thread_id": thread_id}

    def find(self, title: str) -> list[int]:
        matches = []

        def visit(hwnd, _):
            try:
                self.snapshot(int(hwnd), title)
                matches.append(int(hwnd))
            except WindowControlError:
                pass
            return True

        # No unrelated window captions are collected, returned or logged.
        callback = self.callback_type(visit)
        if not self.api.EnumWindows(callback, 0):
            raise WindowControlError("window_enumeration_failed")
        return matches

    def visible(self, hwnd: int) -> bool:
        if not self.exists(hwnd):
            raise WindowControlError("owned_window_missing")
        return bool(self.api.IsWindowVisible(hwnd))

    def set_visible(self, hwnd: int, visible: bool) -> None:
        # ShowWindow returns the *previous* visibility, not success/failure.
        self.api.ShowWindow(hwnd, SW_RESTORE if visible else SW_HIDE)

    def iconic(self, hwnd: int) -> bool:
        if not self.exists(hwnd):
            raise WindowControlError("owned_window_missing")
        return bool(self.api.IsIconic(hwnd))


def _native_api() -> _Windows:
    return _Windows()


def _capture(native: _Windows, hwnd: int, title: str) -> dict[str, Any]:
    window = native.snapshot(hwnd, title)
    return {**window, **_chrome_identity(window["pid"])}


def _bound_identity(state: dict[str, Any]) -> dict[str, Any] | None:
    value = state.get("native")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or any(type(value.get(key)) is not int or value[key] <= 0 for key in ("hwnd", "pid", "thread_id"))
        or type(value.get("create_time")) not in (float, int)
        or not math.isfinite(value["create_time"])
        or value["create_time"] <= 0
        or not isinstance(value.get("exe"), str)
        or not isinstance(value.get("username"), str)
    ):
        raise WindowControlError("owned_window_identity_invalid")
    return {key: value[key] for key in ("hwnd", "pid", "thread_id", "create_time", "exe", "username")}


def _same_window(native: _Windows, state: dict[str, Any], bound: dict[str, Any],
                 *, require_title: bool = True) -> None:
    if require_title:
        current = _capture(native, bound["hwnd"], state["title"])
    else:
        # A render lease changes the active tab's caption. Showing still checks
        # the saved HWND, top-level class, thread and full Chrome process identity.
        window = native.bound_snapshot(bound["hwnd"])
        current = {**window, **_chrome_identity(window["pid"])}
    if current != bound:
        raise WindowControlError("owned_window_identity_changed")


def _binding_alive(native: _Windows, bound: dict[str, Any]) -> bool:
    """Distinguish a closed/reused handle from a live window with a different tab."""
    if not native.exists(bound["hwnd"]):
        return False
    current = native.window_process(bound["hwnd"])
    if any(current[key] != bound[key] for key in ("hwnd", "pid", "thread_id")):
        return False
    return _process_start_time(bound["pid"]) == bound["create_time"]


def _result(state: dict[str, Any] | None, *, visible: bool | None = None, verified: bool = False,
            error: str | None = None) -> dict[str, Any]:
    result = {
        "ok": error is None,
        "hidden": visible is False and verified,
        "visible": visible,
        "verified": verified,
        "manual_reveal": bool(state is not None and state.get("manual_reveal") is True),
    }
    if state is not None:
        result.update({key: state[key] for key in ("window_id", "host_tab_id", "title")})
    if error:
        result["error"] = error
    return result


def register(db, window_id: int, host_tab_id: int) -> dict[str, Any]:
    """Issue an unguessable title; repeat registrations of the same IDs are stable."""
    if not _identifier(window_id) or not _identifier(host_tab_id):
        return _result(None, error="invalid_window_registration")
    with _WINDOW_LOCK:
        saved = db.get_state(STATE_KEY)
        same_registration = (_valid_state(saved) and saved["window_id"] == window_id
                             and saved["host_tab_id"] == host_tab_id)
        if _valid_state(saved) and saved.get("native") is not None:
            try:
                bound = _bound_identity(saved)
                native = _native_api()
                if _binding_alive(native, bound):
                    # Do not lose the only restoration handle for a hidden
                    # window, even when its active tab has changed the title.
                    if not same_registration and not native.visible(bound["hwnd"]):
                        return _result(saved, error="owned_window_already_hidden")
                elif same_registration:
                    # Chrome may reuse extension IDs after a browser restart.
                    # A fresh authenticated registration can bind the new host;
                    # hide/show by themselves never search for a replacement.
                    saved.update(native=None, visible=None, hidden=False, verified=False)
                    db.set_state(STATE_KEY, saved)
            except WindowControlError as exc:
                return _result(saved, error=str(exc))
            except (OSError, psutil.Error):
                return _result(saved, error="native_access_failed")
        if same_registration:
            return _result(saved)
        state = {"window_id": window_id, "host_tab_id": host_tab_id,
                 "title": TITLE_PREFIX + secrets.token_hex(16), "native": None,
                 "visible": None, "hidden": False, "verified": False, "manual_reveal": False}
        db.set_state(STATE_KEY, state)
        return _result(state)


def _pending_recovery_matches(recovery: Any, nonce: Any) -> bool:
    return (
        isinstance(recovery, dict)
        and recovery.get("completed") is False
        and isinstance(nonce, str)
        and re.fullmatch(r"[0-9a-f]{32}", nonce) is not None
        and recovery.get("nonce") == nonce
    )


def retire_for_recovery(db, window_id: int, host_tab_id: int, nonce: str) -> dict[str, Any]:
    """Release one registered window only after a requested recovery restores it.

    A personal tab may have entered the old window. Preserve its native binding
    until the full saved identity is checked and SW_RESTORE is confirmed; the
    authenticated extension can then move its host and register a new window.
    """
    if not _identifier(window_id) or not _identifier(host_tab_id):
        return _result(None, error="invalid_window_registration")
    with _WINDOW_LOCK:
        state = db.get_state(STATE_KEY)
        if not _valid_state(state):
            return _result(None, error="window_not_registered")
        if state["window_id"] != window_id or state["host_tab_id"] != host_tab_id:
            return _result(state, error="recovery_registration_mismatch")
        if not _pending_recovery_matches(db.get_state("browser_model_recovery"), nonce):
            return _result(state, error="recovery_nonce_mismatch")
        if state.get("manual_reveal") is True:
            return _result(state, error="background_window_manual_reveal")
        try:
            bound = _bound_identity(state)
            already_retired = bound is None
            if already_retired:
                proof = state.get("recovery_retirement")
                if (not isinstance(proof, dict) or proof.get("nonce") != nonce
                        or proof.get("window_id") != window_id or proof.get("host_tab_id") != host_tab_id):
                    return _result(state, error="window_not_bound")
                bound = _bound_identity(proof)
                if bound is None:
                    return _result(state, error="window_not_bound")
            native = _native_api()
            _same_window(native, state, bound, require_title=False)
            if not already_retired:
                native.set_visible(bound["hwnd"], True)
            _same_window(native, state, bound, require_title=False)
            actual = native.visible(bound["hwnd"])
            still_minimized = native.iconic(bound["hwnd"])
            _same_window(native, state, bound, require_title=False)
            if not actual or still_minimized:
                raise WindowControlError("window_visibility_not_confirmed")
            # A new recovery request must not be consumed by an old call
            # that was still restoring its window.
            if not _pending_recovery_matches(db.get_state("browser_model_recovery"), nonce):
                return _result(state, error="recovery_nonce_mismatch")
            if not already_retired:
                state = {**state, "native": None, "visible": None, "hidden": False,
                         "verified": False, "checked_at": time.time(), "last_error": None,
                         "recovery_retirement": {"nonce": nonce, "window_id": window_id,
                                                 "host_tab_id": host_tab_id, "native": bound}}
                db.set_state(STATE_KEY, state)
            # A retry rechecks the saved retirement identity and visibility,
            # without touching native state or accepting an unbound window.
            return {**_result(state, visible=True, verified=True), "retired": True,
                    "nonce": nonce, "code": "window_retired_for_recovery"}
        except WindowControlError as exc:
            return _result(state, error=str(exc))
        except (OSError, psutil.Error):
            return _result(state, error="native_access_failed")


def _set_visibility(db, visible: bool) -> dict[str, Any]:
    with _WINDOW_LOCK:
        state = db.get_state(STATE_KEY)
        if not _valid_state(state):
            return _result(None, error="window_not_registered")
        if not visible and "recovery_retirement" in state:
            # A timed-out request may arrive after retirement. Never reacquire
            # the old title and hide a window that can now contain personal tabs.
            # Only registration of different IDs replaces this retirement proof.
            return _result(state, error="background_window_retired_for_recovery")
        if not visible and state.get("manual_reveal") is True:
            return _result(state, error="background_window_manual_reveal")
        try:
            native = _native_api()
            bound = _bound_identity(state)
            if bound is None:
                if visible:
                    return _result(state, error="window_not_bound")
                candidates = native.find(state["title"])
                if not candidates:
                    raise WindowControlError("owned_window_missing")
                if len(candidates) != 1:
                    raise WindowControlError("owned_window_ambiguous")
                bound = _capture(native, candidates[0], state["title"])
                state["native"] = bound
                # Persist identity before the first native mutation, so a crash
                # cannot leave a hidden window with no recorded restoration key.
                state.update(visible=None, hidden=False, verified=False)
                db.set_state(STATE_KEY, state)
            _same_window(native, state, bound, require_title=not visible)
            native.set_visible(bound["hwnd"], visible)
            _same_window(native, state, bound, require_title=not visible)
            actual = native.visible(bound["hwnd"])
            still_minimized = visible and native.iconic(bound["hwnd"])
            _same_window(native, state, bound, require_title=not visible)
            if actual is not visible or still_minimized:
                raise WindowControlError("window_visibility_not_confirmed")
            state.update(visible=actual, hidden=not actual, verified=True,
                         checked_at=time.time(), last_error=None)
            if visible:
                state["manual_reveal"] = True
            db.set_state(STATE_KEY, state)
            return _result(state, visible=actual, verified=True)
        except WindowControlError as exc:
            error = str(exc)
        except (OSError, psutil.Error):
            error = "native_access_failed"
        state.update(visible=None, hidden=False, verified=False,
                     checked_at=time.time(), last_error=error)
        db.set_state(STATE_KEY, state)
        return _result(state, error=error)


def hide(db) -> dict[str, Any]:
    """Use SW_HIDE and confirm IsWindowVisible is false for the registered window."""
    return _set_visibility(db, False)


def show(db) -> dict[str, Any]:
    """Restore only the previously bound window, even while an owned model tab is active."""
    return _set_visibility(db, True)


def resume(db) -> dict[str, Any]:
    """End an explicit manual reveal; the extension must reselect its host to hide."""
    with _WINDOW_LOCK:
        state = db.get_state(STATE_KEY)
        if not _valid_state(state):
            return _result(None, error="window_not_registered")
        state["manual_reveal"] = False
        db.set_state(STATE_KEY, state)
        return _result(state)
