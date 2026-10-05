# Source: https://github.com/fanyuantaier/wechatauto-replica
# Commit: 91dc0e1a601013b759b261061d8f7642f736af90, Apache-2.0.
# Modified: retained compatibility metadata/PE helpers only; see ACCESSIBILITY_LICENSE.txt.
# Apache-2.0 derived compatibility helpers; see LICENSE. No sending or Narrator code.
from wxauto4 import uia as auto

import ctypes
import json
import os
import re
import struct
import time
from ctypes import wintypes
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
try:
    import win32gui, win32con, win32process, win32api
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False
MAIN_CLASS = 'mmui::MainWindow'
LOGIN_CLASS = 'mmui::LoginWindow'
MAIN_NAMES = ('微信', 'Weixin')
LOGIN_BTN_NAMES = ('进入微信', '登录', '进入')
LOGIN_OUTLINE_CLASS = 'mmui::XOutlineButton'
SEARCH_EDIT_CLASS = 'mmui::XValidatorTextEdit'
SEARCH_EDIT_NAME = '搜索'
SESSION_LIST_AID = 'session_list'
SEARCH_LIST_AID = 'search_list'
RESULT_AID_PREFIX = 'search_item_'
CHAT_INPUT_AID = 'chat_input_field'
MAIN_CLASSES = ('mmui::MainWindow',)
LOGIN_CLASSES = ('mmui::LoginWindow',)
MAIN_NAME_HINTS = ('微信', 'Weixin')
SEARCH_EDIT_CLASSES = ('mmui::XValidatorTextEdit',)
SESSION_LIST_AIDS = ('session_list',)
SEARCH_LIST_AIDS = ('search_list',)
CHAT_INPUT_AIDS = ('chat_input_field',)
SNS_LIST_CLASSES = ('mmui::TimeLineListView',)
SNS_LIST_AIDS = ('sns_list',)
_GATE_BLOCK_WARNED = set()
MAIN_TAB_BAR_CLS = 'mmui::MainTabBar'
TAB_ITEM_CLS = 'mmui::XTabBarItem'
CHAT_TAB_NAME = '微信'
DEFAULT_EXE = 'C:\\Program Files\\Tencent\\Weixin\\Weixin.exe'
SECTION_HEADERS = {'最常使用', '最近使用', '联系人', '群聊', '公众号', '服务号', '订阅号', '聊天记录', '收藏', '功能', '小程序', '最近使用过的小程序', '视频号', '企业微信联系人', '朋友圈', '搜索网络结果', '企业微信'}
SPI_GETSCREENREADER = 70
SPI_SETSCREENREADER = 71
SPIF_SENDCHANGE = 2
QACCESSIBLE_ACTIVE_RVA_BY_VERSION = {'4.1.11.22': 169770424, '4.1.13.65': 182628552, '4.1.15.13': 185818168}
QACCESSIBLE_CORE_STRING = b'qt.accessibility.core'
QACCESSIBLE_GATE_PATTERN = re.compile(b'\\x48\\x85\\xc9\\x0f\\x84....\\x80\\x3d(?P<disp>.{4})\\x00\\x0f\\x84', re.DOTALL)
_VERIFIED_GATE_RVA: Dict[str, int] = {}
GATE_CACHE_FILE = os.path.join(os.path.expanduser('~'), '.wechatauto', 'gate_cache.json')
_GATE_CACHE: Optional[Dict[str, dict]] = None
IMAGE_SCN_MEM_EXECUTE = 536870912
IMAGE_SCN_MEM_WRITE = 2147483648
PROCESS_VM_OPERATION = 8
PROCESS_VM_READ = 16
PROCESS_VM_WRITE = 32
PROCESS_QUERY_INFORMATION = 1024
TH32CS_SNAPMODULE = 8
TH32CS_SNAPMODULE32 = 16
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255
MAX_PATH = 260

class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_ulong), ('th32ModuleID', ctypes.c_ulong), ('th32ProcessID', ctypes.c_ulong), ('GlblcntUsage', ctypes.c_ulong), ('ProccntUsage', ctypes.c_ulong), ('modBaseAddr', ctypes.POINTER(ctypes.c_ubyte)), ('modBaseSize', ctypes.c_ulong), ('hModule', ctypes.c_void_p), ('szModule', ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)), ('szExePath', ctypes.c_wchar * MAX_PATH)]

class WeChatUIA:

    @staticmethod
    def _process_modules(pid: int):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        kernel32.Module32FirstW.restype = wintypes.BOOL
        kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        kernel32.Module32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snapshot == INVALID_HANDLE_VALUE:
            return
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                return
            while True:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                yield (base, int(entry.modBaseSize), entry.szModule, entry.szExePath)
                if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)

    @classmethod
    def _weixin_dll_module(cls, pid: int):
        for base, size, name, path in cls._process_modules(pid) or ():
            if name.lower() == 'weixin.dll':
                return (base, size, path)
        return None

    @staticmethod
    def _pe_sections(data: bytes):
        try:
            pe_off = struct.unpack_from('<I', data, 60)[0]
            if data[pe_off:pe_off + 4] != b'PE\x00\x00':
                return []
            coff = pe_off + 4
            count = struct.unpack_from('<H', data, coff + 2)[0]
            opt_size = struct.unpack_from('<H', data, coff + 16)[0]
            sec_off = coff + 20 + opt_size
            sections = []
            for i in range(count):
                off = sec_off + i * 40
                name = data[off:off + 8].split(b'\x00', 1)[0].decode('ascii', 'ignore')
                virtual_size, virtual_address, raw_size, raw_ptr = struct.unpack_from('<IIII', data, off + 8)
                characteristics = struct.unpack_from('<I', data, off + 36)[0]
                sections.append({'name': name, 'rva': virtual_address, 'vsize': virtual_size, 'raw_size': raw_size, 'raw_ptr': raw_ptr, 'chars': characteristics})
            return sections
        except Exception:
            return []

    @staticmethod
    def _section_for_rva(sections, rva: int):
        for sec in sections:
            size = max(sec['vsize'], sec['raw_size'])
            if sec['rva'] <= rva < sec['rva'] + size:
                return sec
        return None

    @staticmethod
    def _offset_to_rva(sections, offset: int) -> Optional[int]:
        for sec in sections:
            if sec['raw_ptr'] <= offset < sec['raw_ptr'] + sec['raw_size']:
                return sec['rva'] + offset - sec['raw_ptr']
        return None

    @staticmethod
    def _read_process_byte(handle, address: int) -> Optional[int]:
        ctypes.windll.kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        ctypes.windll.kernel32.ReadProcessMemory.restype = wintypes.BOOL
        buf = (ctypes.c_ubyte * 1)()
        read = ctypes.c_size_t(0)
        ok = ctypes.windll.kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(read))
        return int(buf[0]) if ok and read.value == 1 else None

    @staticmethod
    def _write_process_byte(handle, address: int, value: int) -> bool:
        ctypes.windll.kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        ctypes.windll.kernel32.WriteProcessMemory.restype = wintypes.BOOL
        buf = (ctypes.c_ubyte * 1)(value & 255)
        written = ctypes.c_size_t(0)
        ok = ctypes.windll.kernel32.WriteProcessMemory(handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(written))
        return bool(ok and written.value == 1)

    @staticmethod
    def _mmui_present(hwnd: int, timeout: float=1.0) -> bool:
        """校验窗口是否已物化出 mmui 控件（仍是 Qt 空壳时为 False）。"""
        deadline = time.time() + max(0.1, timeout)
        while True:
            try:
                c = auto.ControlFromHandle(hwnd)
            except Exception:
                c = None
            if c is not None:
                if (c.ClassName or '').startswith('mmui::'):
                    return True
                try:
                    kids = c.GetChildren()
                except Exception:
                    kids = []
                for k in kids:
                    if (k.ClassName or '').startswith('mmui::'):
                        return True
            if time.time() >= deadline:
                return False
            time.sleep(0.2)