# Derived from TANGandXUE/wcdb-key-tool, MIT; see WCDB_READER_LICENSE.txt.
"""wcdb-key-tool (Windows) — 微信数据库密钥提取工具

Windows 微信数据库密钥提取工具。完整支持仍在进程内存里缓存明文 raw key
的微信版本（4.0.x 一代）。微信 4.1+ 以后，主路径改为只读运行时
`Config.Cipher` 扫描 + HMAC 校验；`capture-experimental` 仅保留为研究性备用
路线，不是主路径。

Usage:
    python3 wcdb_key_tool_windows.py extract              # 提取密钥（优先 runtime 扫描，老版本回退内存扫描）
    python3 wcdb_key_tool_windows.py capture-experimental  # [研究性备用] 断点抓 passphrase
    python3 wcdb_key_tool_windows.py set-passphrase <64位hex>  # 手动填入已获取的 passphrase
    python3 wcdb_key_tool_windows.py decrypt               # 解密数据库
    python3 wcdb_key_tool_windows.py extract --decrypt     # 提取 + 解密一步完成

Requirements:
    - Python 3.10+（Windows 自带 bcrypt.dll，无需第三方加密库）
    - 建议以管理员身份运行（读取其他进程内存需要足够权限）
    - capture-experimental 需要 cdb.exe（"Debugging Tools for Windows"，
      Windows SDK 的可选组件，或随 WinDbg 安装）

Known Gap:
    研究性断点路线仍保留，但默认不会影响 extract；主路径是只读运行时扫描，
    只在读到的候选值通过 HMAC 校验后才保存。

https://github.com/TANGandXUE/wcdb-key-tool
"""
from __future__ import annotations
import argparse
import ctypes
import glob
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import time
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
_print = lambda *a, **kw: print(*a, flush=True, **kw)
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
if sys.platform == 'win32':
    import ctypes.wintypes as wt
    _bcrypt = ctypes.WinDLL('bcrypt')
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    _bcrypt.BCryptSetProperty.argtypes = [wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptDecrypt.argtypes = [wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]

def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC 解密（无 padding），走 CNG 的 bcrypt.dll。"""
    h_alg = wt.HANDLE()
    status = _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), 'AES', None, 0)
    if status != 0:
        raise RuntimeError(f'BCryptOpenAlgorithmProvider failed: {status:#x}')
    try:
        mode = 'ChainingModeCBC\x00'.encode('utf-16-le')
        status = _bcrypt.BCryptSetProperty(h_alg, 'ChainingMode', mode, len(mode), 0)
        if status != 0:
            raise RuntimeError(f'BCryptSetProperty failed: {status:#x}')
        h_key = wt.HANDLE()
        status = _bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0)
        if status != 0:
            raise RuntimeError(f'BCryptGenerateSymmetricKey failed: {status:#x}')
        try:
            iv_buf = ctypes.create_string_buffer(iv, len(iv))
            out_buf = ctypes.create_string_buffer(len(data))
            result_len = ctypes.c_ulong(0)
            status = _bcrypt.BCryptDecrypt(h_key, data, len(data), None, iv_buf, len(iv), out_buf, len(out_buf), ctypes.byref(result_len), 0)
            if status != 0:
                raise RuntimeError(f'BCryptDecrypt failed: {status:#x}')
            return out_buf.raw[:result_len.value]
        finally:
            _bcrypt.BCryptDestroyKey(h_key)
    finally:
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)

def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes((b ^ 58 for b in salt))
    mac_key = hashlib.pbkdf2_hmac('sha512', enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ:PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64:PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    return hm.digest() == stored_hmac

def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ:PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ:PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return bytes(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        decrypted = aes_cbc_decrypt(enc_key, iv, encrypted)
        return decrypted + b'\x00' * RESERVE_SZ
_bcrypt.BCryptDestroyKey.argtypes = [wt.HANDLE]
_bcrypt.BCryptCloseAlgorithmProvider.argtypes = [wt.HANDLE, ctypes.c_ulong]
