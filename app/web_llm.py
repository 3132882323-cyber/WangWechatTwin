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

# Chrome may sleep the connector and it only polls every 30s, so a claim window
# of 35s silently dropped most replies. 75s covers two poll rounds.
CLAIM_GRACE_SECONDS = 75.0
# A channel that just failed gets a short wait so one broken page cannot
# stall every contact behind it.
FAILED_CHANNEL_GRACE_SECONDS = 15.0
FAILED_CHANNEL_MEMORY_SECONDS = 600.0
# How often a stuck claim is released back to the queue.
REAP_INTERVAL_SECONDS = 30.0
# Finished rows are transport leftovers; keep them briefly for inspection.
FINISHED_ROW_RETENTION_SECONDS = 3 * 86400.0
# Budget must cover the claim window plus real page generation time.
PROVIDER_TIMEOUT_BUDGET = {'deepseek_web': 150.0, 'doubao_web': 200.0}


class WebReplyLLM:
    def __init__(self, config):
        self.config = config
        root = config.resolve(config.paths.browser_bridge)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "jobs.sqlite3"
        self._last_reap = 0.0
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
            if 'selection_mode' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'direct'")
            if 'provider' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'chatgpt'")
            db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status,provider,expires)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def decide(self, system_prompt, user_payload, risk):
        from app.llm import LLMError
        self._reap_stale_claims()
        job_id = uuid.uuid4().hex
        provider_budget=PROVIDER_TIMEOUT_BUDGET.get(self.config.openai.provider,self.config.openai.web_reply_timeout_seconds)
        expires = time.time() + min(self.config.openai.web_reply_timeout_seconds,provider_budget)
        schema = json.dumps(ReplyDecision.model_json_schema(), ensure_ascii=False)
        if self.config.openai.provider in {'deepseek_web','doubao_web'}:
            schema=json.dumps({'action':'send','risk':'low','reply':'实际回复正文','confidence':.9,'reason':'简短理由','facts_to_confirm':[],'memory_updates':[],'media_description':'','media_confidence':0,'sticker_id':''},ensure_ascii=False)
            schema+='\n这是输出示例，不是要复制的内容；action 只能为 send/review/hold/ignore，risk 只能为 low/medium/high/critical。不要输出 JSON Schema 或 properties/defs。'
        selection_mode = 'direct'
        try:
            parsed_payload=json.loads(user_payload)
            contact_name=str(parsed_payload.get('contact_profile',{}).get('name','')).strip()[:128]
            media_paths=parsed_payload.pop('__media_paths',[])
            namespace = parsed_payload.get('conversation_key','')
            is_test=parsed_payload.pop('__is_local_test',False) is True
            selection_mode = 'abc' if parsed_payload.get('reply_selection',{}).get('mode')=='abc' else 'direct'
            if selection_mode=='abc' and self.config.openai.provider in {'deepseek_web','doubao_web'}:
                example={'action':'send','risk':'low','reply':'入选候选的实际正文','confidence':.9,'reason':'简短理由','facts_to_confirm':[],'memory_updates':[],'media_description':'','media_confidence':0,'sticker_id':'','candidates':[{'label':label,'reply':'真实候选正文'+label,'style_match':4,'context_fit':4,'continuation':4,'boundary_respect':5,'invented_facts':False,'risk':'low'} for label in ['A','B','C']],'selected_candidate':'A'}
                schema=json.dumps(example,ensure_ascii=False)+'\n以上只是字段结构示例，不得复制候选正文或分数；三条必须贴合本轮微信信息，判断分数不是概率。'
            if self.config.openai.provider in {'deepseek_web','doubao_web'}:
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
            provider={'deepseek_web':'deepseek','doubao_web':'doubao'}.get(self.config.openai.provider,'chatgpt')
            db.execute("INSERT INTO jobs(id,prompt,status,result,created,expires,conversation_key,images,is_test,provider,contact_name,selection_mode) VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?)", (job_id, prompt, "pending", time.time(), expires,namespace,json.dumps(images),int(is_test),provider,contact_name,selection_mode))
        waiting_started=time.time()
        try:
            while time.time() < expires:
                if self.config.resolve(self.config.paths.pause_file).exists() and not is_test:
                    raise LLMError("已暂停网页回复")
                with self.connect() as db:
                    status, result, browser_meta = db.execute(
                        "SELECT status,result,browser_meta FROM jobs WHERE id=?", (job_id,)).fetchone()
                if status=='pending' and time.time()-waiting_started>self._claim_grace_seconds(provider,waiting_started,expires):
                    # A connector can claim between the read above and here.
                    # Only an atomic cancellation proves that another lane may
                    # safely handle this message without submitting it twice.
                    with self.connect() as db:
                        cancelled = db.execute(
                            "UPDATE jobs SET status='expired',prompt='',images='[]' "
                            "WHERE id=? AND status='pending'", (job_id,)).rowcount
                    if cancelled == 1:
                        raise LLMError('网页未领取任务，已快速转审核，避免长期堵塞', fallback_safe=True)
                    continue
                if status == "done":
                    try:
                        return ReplyDecision.model_validate_json(result)
                    except ValueError as exc:
                        raise LLMError("网页结果格式无效，留待审核") from exc
                if status == "failed":
                    code = ''
                    try:
                        metadata = json.loads(browser_meta or '{}')
                        candidate = metadata.get('failure_code') if isinstance(metadata, dict) else None
                        if isinstance(candidate, str) and re.fullmatch(r'[a-z_]{1,80}', candidate):
                            code = candidate
                    except (TypeError, ValueError):
                        pass
                    suffix = f'（错误码：{code}）' if code else ''
                    raise LLMError("网页回复失败，留待审核；不会切回 Codex" + suffix)
                time.sleep(.25)
            raise LLMError("网页连接未就绪或超时，留待审核；不会切回 Codex")
        finally:
            with self.connect() as db:
                # Personal context need not remain in the transport queue.
                db.execute("UPDATE jobs SET prompt='',images='[]',status=CASE WHEN status IN ('done','failed') THEN status ELSE 'expired' END WHERE id=?", (job_id,))

    def _reap_stale_claims(self):
        """Expire abandoned jobs and clear private payloads from old terminal rows.

        A crashed or reloaded page can leave a claimed slot occupied; a crashed
        backend can leave a pending job with personal context in the payload.
        Neither is eligible for a later send after its deadline.
        """
        now = time.time()
        if now - getattr(self, '_last_reap', 0.0) < REAP_INTERVAL_SECONDS:
            return 0
        try:
            with self.connect() as db:
                released = db.execute(
                    "UPDATE jobs SET status='expired',prompt='',images='[]' "
                    "WHERE status IN ('pending','claimed') AND expires<=?", (now,)).rowcount or 0
                pruned = db.execute(
                    "UPDATE jobs SET prompt='',images='[]' "
                    "WHERE status IN ('done','expired','failed') "
                    "AND created<=? AND (prompt!='' OR images!='[]')",
                    (now - FINISHED_ROW_RETENTION_SECONDS,)).rowcount or 0
            self._last_reap = now
            return released + pruned
        except sqlite3.Error:
            return 0

    def _bridge_health(self, provider):
        """False when this channel failed in the last few minutes.

        A page that just failed will keep failing, so waiting the full window
        would only stall the lane behind it.
        """
        try:
            from app.db import Database
            state_db = Database(self.config.resolve(self.config.paths.database))
            failure = state_db.get_state(provider + '_last_failure') or {}
            if failure.get('is_test') is True:
                return True
            seen = float(failure.get('seen_at') or 0)
            success = state_db.get_state(provider + '_last_success') or {}
            recovered = (success.get('is_test') is False
                         and float(success.get('seen_at') or 0) > seen)
            return not (seen and time.time() - seen < FAILED_CHANNEL_MEMORY_SECONDS and not recovered)
        except Exception:
            # Unknown is not a reason to shorten the wait.
            return True

    def _claim_grace_seconds(self, provider, waiting_started, expires):
        """How long a job may sit unclaimed before it becomes a draft review.

        The connector only polls every 30s and Chrome may sleep it, so 35s lost
        most messages. Wait long enough to cover two poll rounds on a healthy
        channel; fail fast on one that just broke, yet never past the deadline.
        """
        remaining = expires - waiting_started - 5.0
        if remaining <= 0:
            # Too little time left to wait for a claim: let the deadline decide.
            return CLAIM_GRACE_SECONDS
        if not self._bridge_health(provider):
            return min(FAILED_CHANNEL_GRACE_SECONDS, remaining)
        return min(CLAIM_GRACE_SECONDS, remaining)

    def complete(self, job_id, result, browser_meta=None):
        with self.connect() as connection:
            row=connection.execute('SELECT selection_mode,is_test,provider FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row and row[0]=='direct':
            raw=json.loads(result)
            raw.pop('candidates',None);raw.pop('selected_candidate',None)
            result=json.dumps(raw,ensure_ascii=False)
        parsed = ReplyDecision.model_validate_json(result)
        allowed = {'tab_id','slot_id','turn','reused','managed_tabs','conversation_fingerprint','queue_wait_ms','web_reply_ms','bridge_total_ms','restored_from_local_context'}
        meta = {k:v for k,v in (browser_meta or {}).items() if k in allowed and isinstance(v,(int,str,bool))}
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET status='done',result=?,browser_meta=? WHERE id=? AND status IN ('pending','claimed') AND expires>?",
                                 (parsed.model_dump_json(), json.dumps(meta),job_id, time.time())).rowcount
        if updated != 1:
            raise ValueError("网页任务已过期或已完成，不能重复提交")
        if row and row[1] == 0 and row[2] in {'chatgpt', 'deepseek', 'doubao'}:
            # Health is based on an actual production completion, not heartbeat
            # traffic or virtual verification jobs. Telemetry must not undo it.
            try:
                from app.db import Database
                Database(self.config.resolve(self.config.paths.database)).set_state(
                    row[2] + '_last_success', {'seen_at': time.time(), 'is_test': False})
            except (OSError, sqlite3.Error):
                pass
