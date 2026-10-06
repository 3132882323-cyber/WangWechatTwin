import time
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.config import AppConfig
from app.db import Database
from app.webui import create_app
from app.web_llm import WebReplyLLM


def test_push_requires_token_and_contains_no_private_prompt(tmp_path):
    c=AppConfig(project_root=tmp_path,openai={'provider':'web'})
    db=Database(tmp_path/'app.sqlite3');app=create_app(c,db);client=TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/browser-bridge/events') as ws:
            ws.send_json({'token':'wrong'});ws.receive_json()
    q=WebReplyLLM(c)
    with q.connect() as connection:
        connection.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('push','private text','pending',?,?)",(time.time(),time.time()+100))
    token=(c.resolve(c.paths.browser_bridge)/'pairing_token.txt').read_text()
    with client.websocket_connect('/browser-bridge/events') as ws:
        ws.send_json({'token':token})
        assert ws.receive_json()=={'type':'ready'}
