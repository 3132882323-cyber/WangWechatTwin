from __future__ import annotations

import json
from pathlib import Path
import plistlib
import subprocess

import pytest

from app.config import load_config
from scripts import macos


def prepared_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "Mac project & 示例"
    root.mkdir()
    (root / "scripts").mkdir()
    executable = root / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("test interpreter", encoding="utf-8")
    config = root / macos.CONFIG_NAME
    config.write_text("adapter: mock\nmode: shadow\n", encoding="utf-8")
    return root, config


def ready_mac(monkeypatch) -> None:
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setattr(macos.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(macos, "check_python", lambda *args: None)
    monkeypatch.setattr(macos, "runtime_settings", lambda *args: {
        "adapter": "mock", "mode": "shadow", "web_enabled": True,
        "host": "127.0.0.1", "port": 18769,
    })


def result(*, code: int = 0, error: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, "", error)


def test_macos_sample_config_keeps_native_sending_and_proactive_disabled(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config.macos.example.yaml"
    local = tmp_path / macos.CONFIG_NAME
    local.write_bytes(source.read_bytes())
    config = load_config(local)
    assert config.adapter == "mock" and config.mode == "shadow"
    assert not config.proactive.enabled and config.proactive.review_only
    assert not config.local_api.auto_load and not config.stickers.enabled
    assert not config.wechat.send_holding_on_review
    assert not config.wechat.sender_all_existing_chats
    assert config.wechat.sender_allowed_contacts == []
    assert config.openai.provider == "hybrid_web"
    assert config.web.host == "127.0.0.1" and config.web.port == 18769
    assert config.resolve(config.paths.database) == tmp_path / ".runtime" / "macos" / "drafts.sqlite3"


def test_setup_initializes_once_and_preserves_existing_config(tmp_path):
    (tmp_path / "config.macos.example.yaml").write_text("mode: shadow\n", encoding="utf-8")
    local = tmp_path / macos.CONFIG_NAME
    assert macos.initialize_config(tmp_path, local)
    local.write_text("# user-owned settings\nmode: shadow\n", encoding="utf-8")
    saved = local.read_bytes()
    timestamp = local.stat().st_mtime_ns
    assert not macos.initialize_config(tmp_path, local)
    assert local.read_bytes() == saved and local.stat().st_mtime_ns == timestamp
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("operation", ["setup", "install", "stop", "uninstall"])
def test_non_mac_guard_precedes_all_file_and_launchctl_mutations(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(macos.sys, "platform", "win32")
    root = tmp_path / "untouched"
    monkeypatch.setattr(macos, "launchctl", lambda *args: pytest.fail("must not invoke launchctl"))
    with pytest.raises(macos.MacSetupError, match="只在 macOS"):
        if operation == "setup":
            macos.setup(root, root / macos.CONFIG_NAME)
        elif operation == "install":
            macos.install_agent(root, root / macos.CONFIG_NAME, home=tmp_path / "home")
        else:
            macos.stop_agent(root, home=tmp_path / "home", uninstall=operation == "uninstall")
    assert list(tmp_path.iterdir()) == []


def test_existing_non_mac_venv_is_preserved(monkeypatch, tmp_path):
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "user-file").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(macos.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not recreate venv"))
    with pytest.raises(macos.MacSetupError, match="尚无 Mac 虚拟环境"):
        macos.ensure_venv(tmp_path, "system-python")
    assert (environment / "user-file").read_text() == "keep"


def test_venv_python_path_is_not_resolved_to_the_base_interpreter(tmp_path):
    root, _ = prepared_project(tmp_path)
    assert macos.project_python(root) == root / ".venv" / "bin" / "python"


@pytest.mark.parametrize("version", [[3, 9], [3, 13], [3, 14]])
def test_rejects_unsupported_existing_python_without_overwriting_it(monkeypatch, tmp_path, version):
    monkeypatch.setattr(macos.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(version), ""))
    with pytest.raises(macos.MacSetupError, match="现有环境不会被覆盖"):
        macos.check_python("existing-python", tmp_path)


@pytest.mark.parametrize("change", [
    {"adapter": "history_http_sender"}, {"mode": "full_auto"},
    {"host": "0.0.0.0"}, {"web_enabled": False}, {"port": 65536},
])
def test_runtime_refuses_native_adapter_auto_mode_or_public_listener(monkeypatch, tmp_path, change):
    data = {"adapter": "mock", "mode": "shadow", "host": "127.0.0.1", "port": 18769, "web_enabled": True}
    data.update(change)
    monkeypatch.setattr(macos.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(data), ""))
    with pytest.raises(macos.MacSetupError):
        macos.runtime_settings(Path("venv-python"), tmp_path, tmp_path / "config.yaml")


def test_launchagent_roundtrip_preserves_paths_and_restarts_only_errors(tmp_path):
    root, config = prepared_project(tmp_path)
    python = macos.project_python(root)
    agent = plistlib.loads(plistlib.dumps(macos.make_launch_agent(root, python, config)))
    assert agent["ProgramArguments"] == [str(python), "-u", str(root / "scripts" / "macos.py"), "--config", str(config), "serve"]
    assert agent["WorkingDirectory"] == str(root)
    assert agent["KeepAlive"] == {"SuccessfulExit": False} and agent["RunAtLoad"]
    assert "shell" not in " ".join(agent["ProgramArguments"])
    assert agent["StandardErrorPath"].startswith(str(root / ".runtime" / "macos"))


def test_explicit_install_loads_agent_once_and_keeps_existing_config(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    saved_config = config.read_bytes()
    calls = []
    loaded = False

    def ctl(*arguments):
        nonlocal loaded
        calls.append(arguments)
        if arguments[0] == "print":
            return result(code=0 if loaded else 1)
        if arguments[0] == "bootstrap":
            loaded = True
        return result()

    monkeypatch.setattr(macos, "launchctl", ctl)
    home = tmp_path / "home"
    path = macos.install_agent(root, config, home=home)
    saved_agent = path.read_bytes()
    assert macos.install_agent(root, config, home=home) == path
    assert path.read_bytes() == saved_agent and config.read_bytes() == saved_config
    assert [call[0] for call in calls] == ["print", "enable", "bootstrap", "print"]
    assert calls[2] == ("bootstrap", "gui/501", str(path))


def test_install_does_not_replace_an_agent_from_another_checkout(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    home = tmp_path / "home"
    path = macos.agent_path(home)
    path.parent.mkdir(parents=True)
    other = tmp_path / "another-checkout"
    path.write_bytes(plistlib.dumps(macos.make_launch_agent(other, other / ".venv" / "bin" / "python", other / macos.CONFIG_NAME)))
    saved = path.read_bytes()
    monkeypatch.setattr(macos, "launchctl", lambda *args: pytest.fail("must not touch foreign job"))
    with pytest.raises(macos.MacSetupError, match="不属于此目录"):
        macos.install_agent(root, config, home=home)
    assert path.read_bytes() == saved


def test_install_does_not_replace_existing_agent_settings(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    home = tmp_path / "home"
    path = macos.agent_path(home)
    path.parent.mkdir(parents=True)
    existing = macos.make_launch_agent(root, macos.project_python(root), root / "another.local.yaml")
    path.write_bytes(plistlib.dumps(existing))
    saved = path.read_bytes()
    monkeypatch.setattr(macos, "launchctl", lambda *args: pytest.fail("must not change existing job"))
    with pytest.raises(macos.MacSetupError, match="未覆盖"):
        macos.install_agent(root, config, home=home)
    assert path.read_bytes() == saved


def test_failed_bootstrap_retains_reviewable_agent(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(macos, "launchctl", lambda *args: result(code=1 if args[0] in {"print", "bootstrap"} else 0, error="bootstrap unavailable"))
    with pytest.raises(macos.MacSetupError, match="bootstrap 失败"):
        macos.install_agent(root, config, home=home)
    assert macos.owned_agent(macos.agent_path(home), root) is not None


def test_stop_and_uninstall_touch_only_owned_agent_and_keep_pause_and_data(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    runtime = root / ".runtime" / "macos"
    runtime.mkdir(parents=True)
    pause = runtime / "PAUSE"
    database = runtime / "drafts.sqlite3"
    pause.write_text("manual pause", encoding="utf-8")
    database.write_bytes(b"existing user data")
    home = tmp_path / "home"
    path = macos.agent_path(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(macos.make_launch_agent(root, macos.project_python(root), config)))
    calls = []
    monkeypatch.setattr(macos, "launchctl", lambda *args: calls.append(args) or result())
    macos.stop_agent(root, home=home)
    assert path.exists()
    macos.stop_agent(root, home=home, uninstall=True)
    assert not path.exists()
    assert database.read_bytes() == b"existing user data" and pause.read_text() == "manual pause"
    assert config.exists()
    assert [call[0] for call in calls] == ["print", "bootout", "print", "bootout"]
    assert all(call[1] == "gui/501/" + macos.LABEL for call in calls)


def test_failed_stop_preserves_agent_instead_of_claiming_uninstall(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    home = tmp_path / "home"
    path = macos.agent_path(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(macos.make_launch_agent(root, macos.project_python(root), config)))
    monkeypatch.setattr(macos, "launchctl", lambda *args: result(code=2 if args[0] == "bootout" else 0, error="not permitted"))
    with pytest.raises(macos.MacSetupError, match="文件仍保留"):
        macos.stop_agent(root, home=home, uninstall=True)
    assert path.exists()


def test_launchd_serve_revalidates_then_executes_draft_only_backend(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, config = prepared_project(tmp_path)
    monkeypatch.setattr(macos, "ROOT", root)
    monkeypatch.setattr(macos.os, "chdir", lambda *args: None)
    calls = []

    class Executed(RuntimeError):
        pass

    def execute(executable, arguments):
        calls.append((executable, arguments))
        raise Executed

    monkeypatch.setattr(macos.os, "execv", execute)
    with pytest.raises(Executed):
        macos.main(["serve"])
    assert calls == [(str(root / ".venv" / "bin" / "python"), macos.app_command(root / ".venv" / "bin" / "python", config, "run", "--mode", "shadow"))]


def test_launchd_refuses_changed_adapter_before_starting_any_backend(monkeypatch, tmp_path):
    ready_mac(monkeypatch)
    root, _ = prepared_project(tmp_path)
    monkeypatch.setattr(macos, "ROOT", root)
    monkeypatch.setattr(macos.os, "execv", lambda *args: pytest.fail("must not launch native adapter"))

    def refused(*args):
        raise macos.MacSetupError("Mac 草稿版只支持 adapter: mock")

    monkeypatch.setattr(macos, "runtime_settings", refused)
    assert macos.main(["serve"]) == 2
