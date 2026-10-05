"""Local queue for ordinary ChatGPT browser replies. Never falls back to Codex."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from app.models import ReplyDecision


class WebReplyLLM:
    def __init__(self, config):
        self.config = config
        root = config.resolve(config.paths.browser_bridge)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "jobs.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, prompt TEXT, status TEXT, result TEXT, created REAL, expires REAL)")
            if 'is_test' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def decide(self, system_prompt, user_payload, risk):
        from app.llm import LLMError
        job_id = uuid.uuid4().hex
        expires = time.time() + self.config.openai.web_reply_timeout_seconds
        schema = json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
        prompt = system_prompt + "\n\n以下是本轮微信数据：\n" + user_payload
        prompt += "\n\n只输出符合以下结构的 JSON，不加代码围栏：\n" + schema
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,prompt,status,result,created,expires) VALUES(?,?,?,NULL,?,?)", (job_id, prompt, "pending", time.time(), expires))
        try:
            while time.time() < expires:
                if self.config.resolve(self.config.paths.pause_file).exists():
                    raise LLMError("已暂停网页回复")
                with self.connect() as db:
                    status, result = db.execute("SELECT status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
                if status == "done":
                    try:
                        return ReplyDecision.model_validate_json(result)
                    except ValueError as exc:
                        raise LLMError("网页结果格式无效，留待审核") from exc
                if status == "failed":
                    raise LLMError("网页回复失败，留待审核；不会切回 Codex")
                time.sleep(.25)
            raise LLMError("网页连接未就绪或超时，留待审核；不会切回 Codex")
        finally:
            with self.connect() as db:
                # Personal context need not remain in the transport queue.
                db.execute("UPDATE jobs SET prompt='',status=CASE WHEN status='done' THEN status ELSE 'expired' END WHERE id=?", (job_id,))

    def complete(self, job_id, result):
        parsed = ReplyDecision.model_validate_json(result)
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET status='done',result=? WHERE id=? AND status IN ('pending','claimed') AND expires>?",
                                 (parsed.model_dump_json(), job_id, time.time())).rowcount
        if updated != 1:
            raise ValueError("网页任务已过期或已完成，不能重复提交")
