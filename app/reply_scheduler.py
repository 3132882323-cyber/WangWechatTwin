"""Durable reply generation queue; polling remains independent of model latency."""
import json
import threading
import time
from contextlib import nullcontext

from app.models import IncomingMessage


class ReplyScheduler:
    FETCH_BATCH_SIZE = 32
    MAX_PAYLOAD_CHARS = 262144
    HEALTH_INTERVAL_SECONDS = 5
    LANES = ('deepseek', 'chatgpt', 'doubao')

    def __init__(self, config, db, pipeline, adapter):
        self.config, self.db, self.pipeline, self.adapter = config, db, pipeline, adapter
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._health_lock = threading.Lock()
        self._errors = {}
        self._exited = set()
        self._started = False
        self._next_health_at = 0
        self._web_queue = None
        if config.openai.provider in {'web', 'deepseek_web', 'doubao_web', 'hybrid_web'}:
            from app.web_llm import WebReplyLLM
            self._web_queue = WebReplyLLM(config)
        with db.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS reply_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, contact TEXT, payload TEXT,
                status TEXT, created REAL, updated REAL)''')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_reply_jobs_status_id ON reply_jobs(status,id)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_reply_jobs_contact_status_id ON reply_jobs(contact,status,id)')
            connection.execute('BEGIN IMMEDIATE')
            self._quarantine_oversized(connection)
            for status in ('working', 'failed'):
                for identity, payload in self._job_rows(connection, status):
                    try:
                        message = IncomingMessage.model_validate_json(payload)
                        origin = json.loads(message.raw_summary or '{}')
                        if not isinstance(origin, dict):
                            raise ValueError('invalid message metadata')
                        if origin.get('merge_truncated'):
                            self.db.quarantine_reply_job(connection, identity, '合并消息超过保留上限，内容不完整')
                            continue
                        origin['resume_incomplete'] = True
                        message = message.model_copy(update={'raw_summary': json.dumps(origin)})
                    except (ValueError, TypeError, AttributeError):
                        # Preserve corrupt payloads without blocking every worker.
                        connection.execute("UPDATE reply_jobs SET status='invalid',updated=? WHERE id=?", (time.time(), identity))
                        continue
                    connection.execute(
                        "UPDATE reply_jobs SET status='pending',payload=?,updated=? WHERE id=?",
                        (message.model_dump_json(), time.time(), identity),
                    )
            connection.execute('COMMIT')
        self.threads = [threading.Thread(target=self._worker_entry, args=(lane,), daemon=True,
                                        name='wechat-reply-' + lane) for lane in self.LANES]
        self.thread = self.threads[0]
        for thread in self.threads:
            thread.start()
        self._started = True
        self._publish_health(force=True)

    @classmethod
    def _job_rows(cls, connection, status, *, contact=None, before=None, descending=False):
        """Read bounded pages, including when an old backlog spans many lanes."""
        last_id = 9223372036854775807 if descending else 0
        comparison, order = ('<', 'DESC') if descending else ('>', 'ASC')
        while True:
            sql = f'SELECT id,payload FROM reply_jobs WHERE status=? AND id{comparison}? AND length(payload)<=?'
            params = [status, last_id, cls.MAX_PAYLOAD_CHARS]
            if contact is not None:
                sql += ' AND contact=?'
                params.append(contact)
            if before is not None:
                sql += ' AND updated<?'
                params.append(before)
            sql += f' ORDER BY id {order} LIMIT ?'
            params.append(cls.FETCH_BATCH_SIZE)
            cursor = connection.execute(sql, params)
            batch = cursor.fetchmany(cls.FETCH_BATCH_SIZE)
            cursor.close()
            if not batch:
                return
            last_id = batch[-1][0]
            yield from batch

    def _quarantine_oversized(self, connection):
        for status in ('pending', 'working', 'failed'):
            while True:
                cursor = connection.execute(
                    'SELECT id FROM reply_jobs WHERE status=? AND length(payload)>? ORDER BY id LIMIT ?',
                    (status, self.MAX_PAYLOAD_CHARS, self.FETCH_BATCH_SIZE),
                )
                rows = cursor.fetchmany(self.FETCH_BATCH_SIZE)
                cursor.close()
                if not rows:
                    break
                for (identity,) in rows:
                    self.db.quarantine_reply_job(connection, identity, '待处理消息批次过大，已隔离')

    @classmethod
    def _review_reason(cls, message, payload):
        if len(payload) > cls.MAX_PAYLOAD_CHARS:
            return '待处理消息批次过大，已隔离'
        try:
            origin = json.loads(message.raw_summary or '{}')
        except (ValueError, TypeError):
            return ''
        return '合并消息超过保留上限，内容不完整' if isinstance(origin, dict) and origin.get('merge_truncated') else ''

    @staticmethod
    def category(message):
        return 'text' if message.message_type == 'text' else 'visual'

    @staticmethod
    def recovering(message):
        origin = json.loads(message.raw_summary or '{}')
        return bool(origin.get('resume_incomplete')) if isinstance(origin, dict) else False

    @classmethod
    def compatible(cls, left, right):
        return not cls.recovering(left) and not cls.recovering(right) and cls.category(left) == cls.category(right)

    @classmethod
    def occupancy_key(cls, message):
        # Recovery can only draft; it must not hold another model's live replies.
        return message.contact, cls.category(message), cls.recovering(message)

    def health(self):
        with self._health_lock:
            exited, errors = set(self._exited), dict(self._errors)
        workers = {lane: thread.is_alive() and lane not in exited
                   for lane, thread in zip(self.LANES, self.threads)}
        dead = [lane for lane, alive in workers.items() if not alive]
        status = ('stopped' if self.stop.is_set() else 'starting' if not self._started
                  else 'degraded' if dead else 'running')
        return {'status': status, 'workers': workers, 'dead_lanes': dead,
                'last_error': errors, 'seen_at': time.time()}

    def _publish_health(self, *, force=False):
        # Diagnostics cannot become a second exception while handling an OOM.
        try:
            with self._health_lock:
                now = time.monotonic()
                if not force and now < self._next_health_at:
                    return
                self._next_health_at = now + self.HEALTH_INTERVAL_SECONDS
            self.db.set_state('reply_scheduler', self.health())
        except Exception:
            pass

    def _event(self, event_type, detail, *, level='info', contact=None):
        try:
            self.db.add_event(event_type, detail, level=level, contact=contact)
        except Exception:
            pass

    def _remember_error(self, lane, error, cleanup_error=None):
        try:
            with self._health_lock:
                self._errors[lane] = {'error': type(error).__name__,
                                      'cleanup_error': type(cleanup_error).__name__ if cleanup_error else None,
                                      'at': time.time()}
        except Exception:
            pass

    def _worker_entry(self, lane):
        try:
            self.run(lane)
        except BaseException as error:
            self._remember_error(lane, error)
        finally:
            try:
                with self._health_lock:
                    self._exited.add(lane)
            except Exception:
                pass
            if not self.stop.is_set():
                self._event('reply_worker_stopped', lane, level='error')
            self._publish_health(force=True)

    def _compatible_job(self, connection, status, message):
        for identity, payload in self._job_rows(connection, status, contact=message.contact, descending=True):
            try:
                previous = IncomingMessage.model_validate_json(payload)
                if self.compatible(previous, message):
                    return identity, previous
            except (ValueError, TypeError, AttributeError):
                continue
        return None

    def submit(self, messages):
        from app.cli import merge_incoming_messages
        self._publish_health()
        now = time.time()
        for message in messages:
            if self.db.seen(message.external_id):
                continue
            with self.db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                self._quarantine_oversized(connection)
                pending = self._compatible_job(connection, 'pending', message)
                if pending:
                    merged = merge_incoming_messages([pending[1], message])[0]
                    payload = merged.model_dump_json()
                    connection.execute('UPDATE reply_jobs SET payload=?,updated=? WHERE id=?',
                                       (payload, now, pending[0]))
                    identity, queued = pending[0], merged
                else:
                    active = self._compatible_job(connection, 'working', message)
                    if active:
                        message = merge_incoming_messages([active[1], message])[0]
                    payload = message.model_dump_json()
                    cursor = connection.execute("INSERT INTO reply_jobs(contact,payload,status,created,updated) VALUES(?,?,'pending',?,?)",
                                                (message.contact, payload, now, now))
                    identity, queued = cursor.lastrowid, message
                reason = self._review_reason(queued, payload)
                if reason:
                    self.db.quarantine_reply_job(connection, identity, reason)
                connection.execute('COMMIT')
        self.wake.set()

    @staticmethod
    def lane(message):
        from app.llm import ReplyLLM
        from app.risk import assess_risk
        return ReplyLLM.route(json.dumps({'incoming': {'content': message.content, 'message_type': message.message_type},
                                         '__media_paths': message.media_paths}), assess_risk(message.content).level)

    def _occupied(self, connection, message):
        wanted = self.occupancy_key(message)
        for _, payload in self._job_rows(connection, 'working', contact=message.contact):
            try:
                if self.occupancy_key(IncomingMessage.model_validate_json(payload)) == wanted:
                    return True
            except (ValueError, TypeError, AttributeError):
                # Undecodable in-flight work cannot permit another send for
                # the same contact while its outcome is uncertain.
                return True
        return False

    def _claim(self, lane):
        row = recovery = None
        with self.db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._quarantine_oversized(connection)
            for identity, payload in self._job_rows(connection, 'pending', before=time.time() - .35):
                try:
                    message = IncomingMessage.model_validate_json(payload)
                    reason = self._review_reason(message, payload)
                    if reason:
                        self.db.quarantine_reply_job(connection, identity, reason)
                        continue
                    recovered = self.recovering(message)
                    if self.lane(message) != lane or self._occupied(connection, message):
                        continue
                except (ValueError, TypeError, AttributeError):
                    connection.execute("UPDATE reply_jobs SET status='invalid',updated=? WHERE id=?", (time.time(), identity))
                    continue
                if recovered:
                    if recovery is None:
                        recovery = identity, payload
                else:
                    row = identity, payload
                    break
            row = row or recovery
            if row:
                connection.execute("UPDATE reply_jobs SET status='working',updated=? WHERE id=?", (time.time(), row[0]))
            connection.execute('COMMIT')
        return row

    def _auto_resume(self):
        """Heal a pause that repeated browser failures created, after a cooldown.

        Only an unchanged pause owned by browser_health can be resumed.
        """
        now = time.time()
        if now - getattr(self, '_last_resume_check', 0.0) < 30:
            return
        self._last_resume_check = now
        try:
            from app.browser_health import auto_resume
            # browser_health records the successful transition once. The
            # scheduler's lane() classifier requires an incoming message.
            auto_resume(self.config, self.db)
        except Exception as error:
            self._remember_error(getattr(self, 'lane', 'deepseek') if not callable(getattr(self, 'lane', None)) else 'deepseek', error)

    def run(self, lane='deepseek'):
        while not self.stop.is_set():
            row = None
            try:
                self._publish_health()
                self._auto_resume()
                if lane == 'deepseek' and self._web_queue is not None:
                    # Keep browser transport clean even while no replies arrive.
                    self._web_queue._reap_stale_claims()
                if (self.db.get_state('resource_pressure') or {}).get('active'):
                    self.stop.wait(2)
                    continue
                if lane == 'chatgpt' and (self.db.get_state('browser_failures') or {}).get('blocked_until', 0) > time.time():
                    self.stop.wait(.5)
                    continue
                if self.config.resolve(self.config.paths.pause_file).exists():
                    self.wake.wait(.5)
                    self.wake.clear()
                    continue
                if lane == 'deepseek':
                    self.pipeline.send_approved(self.adapter)
                row = self._claim(lane)
                if not row:
                    self.wake.wait(.5)
                    self.wake.clear()
                    continue
                message = IncomingMessage.model_validate_json(row[1])
                bind = self.adapter.bind_reply(message) if hasattr(self.adapter, 'bind_reply') else nullcontext()
                started = time.monotonic()
                with bind:
                    result = self.pipeline.process(message, self.adapter)
                # Persist completion before optional telemetry after a send.
                with self.db.connect() as connection:
                    connection.execute("UPDATE reply_jobs SET status='done',payload='',updated=? WHERE id=?", (time.time(), row[0]))
                self._event('reply_timing', json.dumps({'processing_ms': round((time.monotonic() - started) * 1000),
                                                      'result': result.status}), contact=message.contact)
                self._event('reply_job_completed', result.status, contact=message.contact)
            except Exception as error:
                cleanup_error = None
                if row:
                    try:
                        with self.db.connect() as connection:
                            connection.execute("UPDATE reply_jobs SET status='failed',updated=? WHERE id=? AND status='working'",
                                               (time.time(), row[0]))
                    except Exception as cleanup:
                        cleanup_error = cleanup
                self._remember_error(lane, error, cleanup_error)
                self._event('reply_worker_error', lane + ':' + type(error).__name__, level='error')
                self._publish_health(force=True)
                if isinstance(error, MemoryError) or cleanup_error is not None:
                    # Process supervision restarts in a fresh address space.
                    # Never re-run an uncertain send in another thread here.
                    return
                self.stop.wait(.5)

    def close(self):
        self.stop.set()
        self.wake.set()
        self._publish_health(force=True)
