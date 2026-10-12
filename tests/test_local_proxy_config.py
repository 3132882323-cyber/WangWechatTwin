import pytest
from pydantic import ValidationError

from app.config import OpenAISettings


def settings(lane="deepseek", **overrides):
    item = {"enabled": True, "base_url": "http://127.0.0.1:19655/v1", "api_key_file": "private/key.txt"}
    item.update(overrides)
    return {"provider": "hybrid_web", "local_proxies": {lane: item}}


def test_existing_config_has_no_proxy_enabled():
    config = OpenAISettings(provider="hybrid_web")
    assert config.local_proxies == {}
    assert config.everyday_lane == 'deepseek'
    assert config.disabled_lanes == []


@pytest.mark.parametrize('value, message', [
    ({'provider': 'hybrid_web', 'disabled_lanes': ['deepseek']}, 'everyday lane cannot be disabled'),
    ({'provider': 'deepseek_web', 'disabled_lanes': ['deepseek']}, 'provider uses a disabled model lane'),
    ({'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'], 'deepseek_drafts': True},
     'disabled DeepSeek cannot generate comparison drafts'),
])
def test_disabled_lanes_reject_active_routes(value, message):
    with pytest.raises(ValidationError, match=message):
        OpenAISettings(**value)


def test_disabled_deepseek_proxy_requires_explicit_disable():
    value = settings()
    value.update(everyday_lane='doubao', disabled_lanes=['deepseek'])
    with pytest.raises(ValidationError, match='disabled lane cannot have an enabled local proxy'):
        OpenAISettings(**value)
    value['local_proxies']['deepseek']['enabled'] = False
    config = OpenAISettings(**value)
    assert config.everyday_lane == 'doubao'
    assert config.disabled_lanes == ['deepseek']


@pytest.mark.parametrize('value', [
    {'everyday_lane': 'chatgpt'}, {'everyday_lane': 'unknown'}, {'disabled_lanes': ['unknown']},
])
def test_rejects_unknown_everyday_or_disabled_lane(value):
    with pytest.raises(ValidationError):
        OpenAISettings(**value)


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://localhost:19655/v1", "http://127.0.0.1:19655/v1/", "http://127.0.0.1:19655/v1?token=x"])
def test_rejects_other_endpoint(url):
    with pytest.raises(ValidationError):
        OpenAISettings(**settings(base_url=url))


def test_rejects_swapped_lane():
    with pytest.raises(ValidationError):
        OpenAISettings(**settings(lane="doubao"))


def test_rejects_enabled_proxy_with_other_provider():
    value = settings()
    value["provider"] = "api"
    with pytest.raises(ValidationError):
        OpenAISettings(**value)


def test_two_explicit_lanes_keep_gpt_out():
    value = settings()
    value["local_proxies"]["doubao"] = {"enabled": True, "base_url": "http://127.0.0.1:19656/v1", "api_key_file": "private/doubao.txt"}
    assert set(OpenAISettings(**value).local_proxies) == {"deepseek", "doubao"}
    value["local_proxies"]["doubao"]["session_db"] = "other.sqlite3"
    with pytest.raises(ValidationError, match="share their session database"):
        OpenAISettings(**value)


def test_health_does_not_claim_inference_from_open_socket(tmp_path, monkeypatch):
    import socket
    import time
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from app.config import AppConfig
    from app.db import Database
    from app.webui import create_app

    value = settings()
    config = AppConfig(project_root=tmp_path, openai=value)
    db = Database(tmp_path / "health.sqlite3")
    db.set_state("browser_bridge", {"ready": True, "heartbeat_seen_at": time.time()})
    db.set_state("doubao_bridge", {"ready": True, "heartbeat_seen_at": time.time()})
    db.set_state("deepseek_bridge", {"ready": False, "blocked": True})
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext())
    client = TestClient(create_app(config, db))
    lane = client.get("/health").json()["model_bridges"]["deepseek"]
    assert lane["transport"] == "local_http_proxy"
    assert lane["transport_reachable"] is True
    assert lane["inference_verified"] is False
    assert lane["status"] == "reachable_unverified"
    assert client.get("/health").json()["ok"] is False

    def unreachable(*args, **kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket, "create_connection", unreachable)
    assert client.get("/health").json()["model_bridges"]["deepseek"]["status"] == "unreachable"
