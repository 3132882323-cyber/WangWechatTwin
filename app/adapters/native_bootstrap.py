"""Reconnect the hash-verified local transport after a WeChat restart.

No window activation, mouse, keyboard, credential entry or GUI fallback.
The manifest and binaries are private local installation artifacts.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

import psutil


class NativeBootstrap:
    def __init__(self, config, reader):
        self.config, self.reader = config, reader
        self.last_check = 0.0
        self.attempted = set()
        self.last_error = ""

    def ensure(self):
        if not self.config.local_api.auto_load or time.monotonic()-self.last_check < 5:
            return
        self.last_check = time.monotonic()
        with socket.socket() as check:
            check.settimeout(.3)
            if check.connect_ex(("127.0.0.1", self.config.local_api.port)) == 0:
                return  # Account identity is still checked before each send.
        manifest_path = self.config.resolve(self.config.local_api.bootstrap_manifest)
        if not manifest_path.exists():
            self.last_error = "缺少已验证接口的本机安装清单"
            return
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = manifest_path.parent.resolve()
        dll = (root/data["dll"]).resolve()
        probe = (root/data["account_probe"]).resolve()
        if dll.parent != root or probe.parent != root:
            raise RuntimeError("接口安装清单路径越界")
        if hashlib.sha256(dll.read_bytes()).hexdigest() != data["dll_sha256"]:
            raise RuntimeError("接口 DLL 与已验证版本不一致，停止加载")
        if hashlib.sha256(probe.read_bytes()).hexdigest() != data["probe_sha256"]:
            raise RuntimeError("账号探针与已验证版本不一致，停止加载")
        for process in psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"]):
            try:
                if process.info["name"].lower() != "weixin.exe" or Path(process.info["exe"]) != Path(data["client_exe"]):
                    continue
                command = " ".join(process.info["cmdline"] or [])
                if "--type=" in command or "--crashpad-handler" in command:
                    continue
                identity = (process.pid, process.info["create_time"])
                if identity in self.attempted:
                    continue
                modules = process.memory_maps()
                client = next((Path(m.path) for m in modules if m.path.lower().endswith("weixin.dll")), None)
                if not client or hashlib.sha256(client.read_bytes()).hexdigest() != data["client_dll_sha256"]:
                    self.last_error = "微信版本变化，接口须重新适配；未启用界面发送"
                    continue
                expected = self.reader.root/"expected_owner.private.txt"
                expected.write_text(self.reader.self_username, encoding="utf-8")
                result = subprocess.run([str(probe), str(process.pid), str(expected)], capture_output=True,
                                        timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0 or not json.loads(result.stdout).get("matches_authenticated_owner"):
                    self.last_error = "微信进程账号与已认证消息库不一致"
                    continue
                self.attempted.add(identity)  # Unknown load outcomes must not be retried.
                self._load(process, dll)
                self.last_error = ""
                return
            except (psutil.Error, OSError, ValueError, subprocess.TimeoutExpired):
                self.last_error = "接口进程暂不可连接，稍后检查"

    @staticmethod
    def _load(process, dll):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "OpenProcess": ([wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE],wintypes.BOOL),
            "VirtualAllocEx": ([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_size_t,wintypes.DWORD,wintypes.DWORD],ctypes.c_void_p),
            "VirtualFreeEx": ([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_size_t,wintypes.DWORD],wintypes.BOOL),
            "WriteProcessMemory": ([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)],wintypes.BOOL),
            "GetModuleHandleW": ([wintypes.LPCWSTR],wintypes.HMODULE),
            "GetProcAddress": ([wintypes.HMODULE,ctypes.c_char_p],ctypes.c_void_p),
            "CreateRemoteThread": ([wintypes.HANDLE,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p],wintypes.HANDLE),
            "WaitForSingleObject": ([wintypes.HANDLE,wintypes.DWORD],wintypes.DWORD),
        }
        for name,(args,result) in declarations.items():
            function=getattr(kernel,name);function.argtypes=args;function.restype=result
        handle=kernel.OpenProcess(0x43a,False,process.pid)
        if not handle:raise ctypes.WinError(ctypes.get_last_error())
        address=None;thread=None;finished=False
        try:
            payload=(str(dll)+"\0").encode("utf-16-le")
            address=kernel.VirtualAllocEx(handle,None,len(payload),0x3000,0x04)
            if not address:raise ctypes.WinError(ctypes.get_last_error())
            count=ctypes.c_size_t();buffer=ctypes.create_string_buffer(payload)
            if not kernel.WriteProcessMemory(handle,address,buffer,len(payload),ctypes.byref(count)) or count.value!=len(payload):
                raise ctypes.WinError(ctypes.get_last_error())
            loader=kernel.GetProcAddress(kernel.GetModuleHandleW("kernel32.dll"),b"LoadLibraryW")
            if not any(int(m.addr,16)<=loader<int(m.addr,16)+m.rss and Path(m.path).name.lower() in
                       {"kernel32.dll","kernelbase.dll"} for m in process.memory_maps(grouped=False)):
                raise RuntimeError("系统模块地址未通过核验")
            thread=kernel.CreateRemoteThread(handle,None,0,loader,address,0,None)
            if not thread:raise ctypes.WinError(ctypes.get_last_error())
            finished=kernel.WaitForSingleObject(thread,10000)==0
            if not finished:raise RuntimeError("接口加载结果未知，不自动重复加载")
        finally:
            if thread:kernel.CloseHandle(thread)
            if address and (finished or not thread):kernel.VirtualFreeEx(handle,address,0,0x8000)
            kernel.CloseHandle(handle)
