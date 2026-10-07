"""Local queue for ordinary ChatGPT browser replies. Never falls back to Codex."""
from __future__ import annotations

import json
import re
import base64
from pathlib import Path
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
            if 'conversation_key' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN conversation_key TEXT NOT NULL DEFAULT ''")
            if 'browser_meta' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN browser_meta TEXT NOT NULL DEFAULT '{}'")
            if 'images' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")
            if 'contact_name' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN contact_name TEXT NOT NULL DEFAULT ''")
            if 'provider' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'chatgpt'")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def decide(self, system_prompt, user_payload, risk):
        from app.llm import LLMError
        job_id = uuid.uuid4().hex
        expires = time.time() + self.config.openai.web_reply_timeout_seconds
        schema = json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
        if self.config.openai.provider=='deepseek_web':
            schema=json.dumps({'action':'send','risk':'low','reply':'实际回复正文','confidence':.9,'reason':'简短理由','facts_to_confirm':[],'memory_updates':[],'media_description':'','media_confidence':0,'sticker_id':''},ensure_ascii=False)
            schema+='\n这是输出示例，不是要复制的内容；action 只能为 send/review/hold/ignore，risk 只能为 low/medium/high/critical。不要输出 JSON Schema 或 properties/defs。'
        try:
            parsed_payload=json.loads(user_payload)
            contact_name=str(parsed_payload.get('contact_profile',{}).get('name','')).strip()[:128]
            media_paths=parsed_payload.pop('__media_paths',[])
            namespace = parsed_payload.get('conversation_key','')
            is_test=parsed_payload.pop('__is_local_test',False) is True
            if self.config.openai.provider=='deepseek_web':
                # Keep current two-way context and a few relevant examples; avoid replaying a giant style report.
                parsed_payload['style_examples']=parsed_payload.get('style_examples',[])[:4]
                parsed_payload['historical_conversation_memory']=parsed_payload.get('historical_conversation_memory',[])[:6]
                parsed_payload['current_personal_conversation']=parsed_payload.get('current_personal_conversation',[])[-10:]
                latest=parsed_payload.get('current_personal_conversation',[])
                present={(row.get('direction'),row.get('content')) for row in latest}
                parsed_payload['recent_messages']=[row for row in parsed_payload.get('recent_messages',[]) if (row.get('direction'),row.get('content')) not in present][-8:]
                parsed_payload['style_examples']=parsed_payload['style_examples'][:4]
                parsed_payload.pop('owner_role_profile',None)
                system_prompt=system_prompt.split('以下是从本人微信文字回复离线统计的表达习惯')[0]
            user_payload=json.dumps(parsed_payload,ensure_ascii=False)
        except (ValueError, AttributeError):
            contact_name=''
            namespace = ''
            media_paths=[]
            is_test=False
        images=[]
        allowed_roots=[(self.config.resolve(self.config.paths.history_reader)/name).resolve() for name in ['sticker_assets','image_assets']]
        for raw_path in media_paths[:3]:
            path=Path(raw_path).resolve()
            if not any(path.is_relative_to(root) for root in allowed_roots) or path.suffix!='.png' or path.stat().st_size>2*1024*1024:
                raise LLMError('表情图片路径或尺寸无效')
            images.append({'name':path.name,'mime':'image/png','data':base64.b64encode(path.read_bytes()).decode('ascii')})
        if not isinstance(namespace,str) or not re.fullmatch(r'[0-9a-f]{64}',namespace):
            namespace = 'isolated:'+job_id
        try:user_payload=json.dumps(json.loads(user_payload),ensure_ascii=False,separators=(',',':'))
        except ValueError:pass
        prompt = system_prompt + "\n\n以下是本轮微信数据：\n" + user_payload
        prompt += "\n\n只输出符合以下结构的 JSON，不加代码围栏：\n" + schema
        prompt += '\n如果需要回应，reply 必须写出真正可用的回复正文；review 表示供人审核，不表示 reply 留空。只有无需回应时才使用 ignore。'
        prompt += '\n本轮资料是当前依据。此专用对话只属于一位微信联系人；旧价格、计划、情绪和承诺仍需按日期核实。只回复本轮消息。'
        prompt += '\n此网页对话里先前的 assistant 回复是模型生成结果，不是新的本人自述或已确认事实。不得把自己的旧输出当作本人原话强化；本人亲自修改和本轮已核验微信资料优先。'
        with self.connect() as db:
            provider='deepseek' if self.config.openai.provider=='deepseek_web' else 'chatgpt'
            db.execute("INSERT INTO jobs(id,prompt,status,result,created,expires,conversation_key,images,is_test,provider,contact_name) VALUES(?,?,?,NULL,?,?,?,?,?,?,?)", (job_id, prompt, "pending", time.time(), expires,namespace,json.dumps(images),int(is_test),provider,contact_name))
        waiting_started=time.time()
        try:
            while time.time() < expires:
                if self.config.resolve(self.config.paths.pause_file).exists():
                    raise LLMError("已暂停网页回复")
                with self.connect() as db:
                    status, result = db.execute("SELECT status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
                if status=='pending' and time.time()-waiting_started>35:
                    raise LLMError('网页未领取任务，已快速转审核，避免长期堵塞')
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
                db.execute("UPDATE jobs SET prompt='',images='[]',status=CASE WHEN status='done' THEN status ELSE 'expired' END WHERE id=?", (job_id,))

    def complete(self, job_id, result, browser_meta=None):
        parsed = ReplyDecision.model_validate_json(result)
        allowed = {'tab_id','slot_id','turn','reused','managed_tabs','conversation_fingerprint','queue_wait_ms','web_reply_ms','bridge_total_ms'}
        meta = {k:v for k,v in (browser_meta or {}).items() if k in allowed and isinstance(v,(int,str,bool))}
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET status='done',result=?,browser_meta=? WHERE id=? AND status IN ('pending','claimed') AND expires>?",
                                 (parsed.model_dump_json(), json.dumps(meta),job_id, time.time())).rowcount
        if updated != 1:
            raise ValueError("网页任务已过期或已完成，不能重复提交")
