import builtins
import hashlib
import hmac
import importlib.util
import sqlite3
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.history_reader import authenticated_snapshot


# NIST SP 800-38A F.2.5: AES-256-CBC, including four chained blocks and no padding.
KEY = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
CIPHERTEXT = bytes.fromhex(
    "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
    "9cfc4e967edb808d679f777bc6702c7d"
    "39f23369a9d9bacfa530e26304231461"
    "b2eb05e2c39be9fcda6c19078c6a9d1b"
)
PLAINTEXT = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)


def portable_module(monkeypatch, platform="darwin"):
    """Execute a fresh non-Windows module import without changing the host OS.

    Crypto's native loader still sees the actual host platform. Only this
    module's sys import is replaced, exercising its missing-Win32-name path.
    """
    path = Path(__file__).parents[1] / "app" / "adapters" / "history_crypto.py"
    spec = importlib.util.spec_from_file_location("history_crypto_" + platform, path)
    module = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sys" and globals and globals.get("__name__") == module.__name__:
            return SimpleNamespace(platform=platform)
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as importing_context:
        importing_context.setattr(builtins, "__import__", importing)
        spec.loader.exec_module(module)
    assert "wt" not in module.__dict__ and "_bcrypt" not in module.__dict__
    return module


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_non_windows_import_and_raw_aes_match_nist_vector(monkeypatch, platform):
    module = portable_module(monkeypatch, platform)
    assert module.aes_cbc_decrypt(KEY, IV, CIPHERTEXT) == PLAINTEXT


@pytest.mark.skipif(sys.platform != "win32", reason="Requires the real Windows CNG library")
def test_windows_cng_still_matches_nist_vector():
    from app.adapters.history_crypto import aes_cbc_decrypt

    assert aes_cbc_decrypt(KEY, IV, CIPHERTEXT) == PLAINTEXT


def test_non_windows_optional_dependency_is_lazy_and_missing_error_is_explicit(monkeypatch):
    original_import = builtins.__import__

    def without_crypto(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "Crypto.Cipher":
            raise ModuleNotFoundError("No module named 'Crypto'", name="Crypto")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    module = portable_module(monkeypatch)
    with pytest.raises(RuntimeError, match="可选依赖 pycryptodome"):
        module.aes_cbc_decrypt(KEY, IV, CIPHERTEXT)


@pytest.mark.parametrize("corrupt_page", [None, 1, 2])
def test_portable_snapshot_keeps_per_page_hmac_before_decryption(tmp_path, monkeypatch, corrupt_page):
    from Crypto.Cipher import AES

    module = portable_module(monkeypatch)
    monkeypatch.setitem(sys.modules, "app.adapters.history_crypto", module)
    salt = bytes(range(16, 32))
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, bytes(value ^ 0x3a for value in salt), 2, dklen=32)
    pages, decoded = [], []
    for number in (1, 2):
        plaintext = bytes([number]) * (4000 if number == 1 else 4016)
        ciphertext = AES.new(KEY, AES.MODE_CBC, iv=IV).encrypt(plaintext)
        payload = ciphertext + IV
        mac = hmac.new(mac_key, payload + struct.pack("<I", number), hashlib.sha512).digest()
        page = (salt if number == 1 else b"") + payload + mac
        if corrupt_page == number:
            page = page[:-1] + bytes([page[-1] ^ 1])
        pages.append(page)
        decoded.append((module.SQLITE_HDR if number == 1 else b"") + plaintext + b"\0" * 80)

    source = tmp_path / "synthetic-encrypted.db"
    source.write_bytes(b"".join(pages))
    output = tmp_path / "snapshot"
    output.mkdir()
    info = {"source": str(source), "enc_key": KEY.hex(), "salt": salt.hex()}
    original_decrypt, calls = module.aes_cbc_decrypt, []

    def decrypt(key, iv, data):
        calls.append(len(data))
        return original_decrypt(key, iv, data)

    monkeypatch.setattr(module, "aes_cbc_decrypt", decrypt)

    class CheckedSyntheticPages:
        def execute(self, query):
            assert query == "PRAGMA quick_check"
            assert corrupt_page is None, "Unauthenticated pages reached the SQLite reader"
            return self

        def fetchall(self):
            return [("ok",)]

        def close(self):
            pass

    # These synthetic encrypted pages exercise crypto/authentication, not the
    # structure of any WeChat database. Existing reader tests cover real SQLite.
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: CheckedSyntheticPages())
    if corrupt_page is None:
        target, _ = authenticated_snapshot(info, output)
        assert target.read_bytes() == b"".join(decoded)
        assert calls == [4000, 4016]
    else:
        with pytest.raises(RuntimeError, match="认证失败"):
            authenticated_snapshot(info, output)
        assert calls == ([] if corrupt_page == 1 else [4000])
