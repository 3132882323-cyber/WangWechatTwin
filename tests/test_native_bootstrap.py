import json
from types import SimpleNamespace

import pytest

from app.adapters.native_bootstrap import NativeBootstrap
from app.config import AppConfig


class OfflineSocket:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def settimeout(self, *_): pass
    def connect_ex(self, *_): return 1


def test_bootstrap_disabled_does_not_open_socket(tmp_path, monkeypatch):
    config=AppConfig(project_root=tmp_path)
    manager=NativeBootstrap(config,SimpleNamespace())
    monkeypatch.setattr('app.adapters.native_bootstrap.socket.socket',lambda:pytest.fail('disabled bootstrap opened socket'))
    manager.ensure()


def test_bootstrap_rejects_manifest_escape(tmp_path, monkeypatch):
    config=AppConfig(project_root=tmp_path,local_api={'auto_load':True,'bootstrap_manifest':'install/manifest.json'})
    root=tmp_path/'install';root.mkdir()
    (root/'manifest.json').write_text(json.dumps({'dll':'../foreign.dll','account_probe':'account_probe.exe'}))
    monkeypatch.setattr('app.adapters.native_bootstrap.socket.socket',OfflineSocket)
    with pytest.raises(RuntimeError,match='路径越界'):
        NativeBootstrap(config,SimpleNamespace()).ensure()


def test_bootstrap_rejects_changed_binary(tmp_path, monkeypatch):
    config=AppConfig(project_root=tmp_path,local_api={'auto_load':True,'bootstrap_manifest':'install/manifest.json'})
    root=tmp_path/'install';root.mkdir();(root/'driver.dll').write_bytes(b'changed')
    (root/'manifest.json').write_text(json.dumps({'dll':'driver.dll','account_probe':'account_probe.exe','dll_sha256':'0'*64}))
    monkeypatch.setattr('app.adapters.native_bootstrap.socket.socket',OfflineSocket)
    with pytest.raises(RuntimeError,match='已验证版本'):
        NativeBootstrap(config,SimpleNamespace()).ensure()
