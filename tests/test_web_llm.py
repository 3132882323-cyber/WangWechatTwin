import pytest
from app.config import AppConfig
from app.llm import ReplyLLM, LLMError
from app.models import RiskLevel


def test_web_never_constructs_codex_or_api(tmp_path,monkeypatch):
    monkeypatch.setattr('app.llm.openai_credentials',lambda: (_ for _ in ()).throw(AssertionError('credentials accessed')))
    config=AppConfig(project_root=tmp_path,openai={'provider':'web','web_reply_timeout_seconds':0})
    llm=ReplyLLM(config)
    with pytest.raises(LLMError,match='不会切回 Codex') as caught:
        llm.decide('prompt','message',RiskLevel.low)
    assert caught.value.fallback_safe is False
    with llm.account.connect() as db:
        assert db.execute('SELECT status,prompt FROM jobs').fetchone()==('expired','')


def test_late_browser_output_cannot_be_replayed(tmp_path):
    from app.web_llm import WebReplyLLM
    queue=WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,result,created,expires) VALUES('old','','claimed',NULL,0,1)")
    with pytest.raises(ValueError):
        queue.complete('old','{"action":"send","risk":"low","reply":"你好"}')


def test_professional_direct_completion_ignores_invalid_unused_candidates(tmp_path):
    import time
    from app.web_llm import WebReplyLLM
    queue = WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as connection:
        connection.execute("INSERT INTO jobs(id,prompt,status,created,expires,selection_mode) VALUES('direct-check','','claimed',?,?,'direct')", (time.time(),time.time()+60))
    queue.complete('direct-check','{"action":"review","risk":"low","reply":"专业直接答案","candidates":"not needed","selected_candidate":"Z"}')
    with queue.connect() as connection:
        raw = connection.execute("SELECT result FROM jobs WHERE id='direct-check'").fetchone()[0]
    assert '专业直接答案' in raw
    assert 'not needed' not in raw


def test_contact_namespace_is_stable_and_unknown_jobs_never_share_chat(tmp_path):
    import json
    c=AppConfig(project_root=tmp_path,openai={'provider':'web','web_reply_timeout_seconds':0})
    llm=ReplyLLM(c)
    for payload in [json.dumps({'conversation_key':'a'*64}), json.dumps({'conversation_key':'a'*64}), '{}', '{}']:
        with pytest.raises(LLMError):llm.decide('s',payload,RiskLevel.low)
    with llm.account.connect() as db:
        namespaces=[r[0] for r in db.execute('SELECT conversation_key FROM jobs ORDER BY created')]
    assert namespaces[:2]==['a'*64,'a'*64]
    assert namespaces[2].startswith('isolated:') and namespaces[2]!=namespaces[3]


def test_browser_completion_metadata_tracks_reuse_without_raw_urls(tmp_path):
    import time,json
    from app.web_llm import WebReplyLLM
    q=WebReplyLLM(AppConfig(project_root=tmp_path))
    with q.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('pool','','claimed',?,?)",(time.time(),time.time()+100))
    q.complete('pool','{"action":"review","risk":"low","reply":"第二轮"}',{'tab_id':1,'turn':2,'reused':True,'raw_url':'private-url'})
    with q.connect() as db:meta=json.loads(db.execute("SELECT browser_meta FROM jobs WHERE id='pool'").fetchone()[0])
    assert meta['reused'] is True and meta['turn']==2 and 'raw_url' not in meta


def test_pause_allows_only_explicit_virtual_queue_verification(tmp_path,monkeypatch):
    import json
    from app.web_llm import WebReplyLLM
    cfg=AppConfig(project_root=tmp_path,openai={'provider':'deepseek_web','web_reply_timeout_seconds':2})
    cfg.resolve(cfg.paths.pause_file).parent.mkdir(parents=True,exist_ok=True);cfg.resolve(cfg.paths.pause_file).touch()
    llm=WebReplyLLM(cfg)
    def finish(_):
        with llm.connect() as conn:
            row=conn.execute("SELECT id,is_test FROM jobs WHERE status='pending' ORDER BY created DESC LIMIT 1").fetchone()
        assert row[1]==1
        llm.complete(row[0],json.dumps({'action':'review','risk':'low','reply':'测试完成'}))
    monkeypatch.setattr('app.web_llm.time.sleep',finish)
    result=llm.decide('test',json.dumps({'__is_local_test':True,'incoming':{'content':'虚构'}}),RiskLevel.low)
    assert result.reply=='测试完成'
    with pytest.raises(LLMError,match='已暂停'):llm.decide('production',json.dumps({'incoming':{'content':'正常消息'}}),RiskLevel.low)


def test_failed_visual_job_retains_failure_status_and_clears_media(tmp_path,monkeypatch):
    import json
    from PIL import Image
    from app.web_llm import WebReplyLLM

    cfg=AppConfig(project_root=tmp_path,openai={'provider':'doubao_web','web_reply_timeout_seconds':2})
    image_dir=cfg.resolve(cfg.paths.history_reader)/'sticker_assets'
    image_dir.mkdir(parents=True)
    image_path=image_dir/'frame.png'
    Image.new('RGB',(2,2),'white').save(image_path)
    llm=WebReplyLLM(cfg)

    def fail_claimed_job(_):
        with llm.connect() as db:
            row=db.execute("SELECT id,images FROM jobs WHERE status='pending'").fetchone()
            assert row is not None and len(json.loads(row[1]))==1
            db.execute("UPDATE jobs SET status='failed' WHERE id=?",(row[0],))

    monkeypatch.setattr('app.web_llm.time.sleep',fail_claimed_job)
    payload=json.dumps({'__media_paths':[str(image_path)],'incoming':{'message_type':'sticker','content':'[表情包]'}})
    with pytest.raises(LLMError,match='网页回复失败') as caught:
        llm.decide('visual',payload,RiskLevel.low)
    assert caught.value.fallback_safe is False
    with llm.connect() as db:
        assert db.execute('SELECT status,prompt,images FROM jobs').fetchone()==('failed','','[]')


def test_stale_claimed_job_is_released_so_the_lane_can_move_on(tmp_path):
    import time
    from app.web_llm import WebReplyLLM
    queue=WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('stuck','','claimed',?,?)",(time.time()-100,time.time()-1))
    # A page that died after claiming must not hold its slot forever.
    assert queue._reap_stale_claims()==1
    with queue.connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE id='stuck'").fetchone()[0]=='expired'


def test_reap_is_throttled_so_every_message_does_not_hit_the_db(tmp_path):
    import time
    from app.web_llm import WebReplyLLM
    queue=WebReplyLLM(AppConfig(project_root=tmp_path))
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('a','','claimed',?,?)",(time.time()-100,time.time()-1))
    assert queue._reap_stale_claims()==1
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires) VALUES('b','','claimed',?,?)",(time.time()-100,time.time()-1))
    # Right after a reap the throttle skips the work, leaving the row alone.
    assert queue._reap_stale_claims()==0
    with queue.connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE id='b'").fetchone()[0]=='claimed'


def test_unclaimed_job_only_allows_fallback_after_atomic_cancellation(tmp_path, monkeypatch):
    from app.web_llm import WebReplyLLM

    queue = WebReplyLLM(AppConfig(project_root=tmp_path,
                                  openai={'provider': 'deepseek_web', 'web_reply_timeout_seconds': 1}))
    monkeypatch.setattr(queue, '_claim_grace_seconds', lambda *args: -1)
    with pytest.raises(LLMError, match='网页未领取任务') as caught:
        queue.decide('prompt', '{}', RiskLevel.low)
    assert caught.value.fallback_safe is True
    with queue.connect() as db:
        assert db.execute('SELECT status,prompt,images FROM jobs').fetchone() == ('expired', '', '[]')


def test_claim_winning_after_pending_read_forbids_fallback(tmp_path, monkeypatch):
    from app.web_llm import WebReplyLLM

    queue = WebReplyLLM(AppConfig(project_root=tmp_path,
                                  openai={'provider': 'deepseek_web', 'web_reply_timeout_seconds': .2}))
    claimed = False

    def claim_before_cancel(*args):
        nonlocal claimed
        if not claimed:
            with queue.connect() as db:
                assert db.execute("UPDATE jobs SET status='claimed' WHERE status='pending'").rowcount == 1
            claimed = True
        return -1

    monkeypatch.setattr(queue, '_claim_grace_seconds', claim_before_cancel)
    with pytest.raises(LLMError, match='超时') as caught:
        queue.decide('prompt', '{}', RiskLevel.low)
    assert claimed
    assert caught.value.fallback_safe is False
    with queue.connect() as db:
        assert db.execute('SELECT status,prompt,images FROM jobs').fetchone() == ('expired', '', '[]')


def test_reap_expires_abandoned_pending_without_touching_future_work(tmp_path):
    import time
    from app.web_llm import FINISHED_ROW_RETENTION_SECONDS, WebReplyLLM

    queue = WebReplyLLM(AppConfig(project_root=tmp_path))
    now = time.time()
    with queue.connect() as db:
        indexes = {row[1] for row in db.execute('PRAGMA index_list(jobs)')}
        assert {'idx_jobs_queue', 'idx_jobs_created'} <= indexes
        rows = [
            ('abandoned', 'private', 'pending', now - 100, now - 1, '["private-image"]'),
            ('future', 'keep', 'pending', now, now + 100, '["keep-image"]'),
            ('old-done', 'private', 'done', now - FINISHED_ROW_RETENTION_SECONDS - 1,
             now - 1, '["private-image"]'),
            ('recent-done', 'keep', 'done', now, now + 100, '["keep-image"]'),
        ]
        db.executemany('INSERT INTO jobs(id,prompt,status,created,expires,images) VALUES(?,?,?,?,?,?)', rows)

    assert queue._reap_stale_claims() == 2
    with queue.connect() as db:
        after = {row[0]: row[1:] for row in db.execute('SELECT id,status,prompt,images FROM jobs')}
    assert after['abandoned'] == ('expired', '', '[]')
    assert after['old-done'] == ('done', '', '[]')
    assert after['future'] == ('pending', 'keep', '["keep-image"]')
    assert after['recent-done'] == ('done', 'keep', '["keep-image"]')


def test_scheduler_reaps_web_queue_while_idle_and_paused(tmp_path):
    import time
    from types import SimpleNamespace
    from app.db import Database
    from app.reply_scheduler import ReplyScheduler
    from app.web_llm import WebReplyLLM

    config = AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'})
    pause = config.resolve(config.paths.pause_file)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.touch()
    queue = WebReplyLLM(config)
    with queue.connect() as db:
        db.execute("INSERT INTO jobs(id,prompt,status,created,expires,images) "
                   "VALUES('idle-stale','private','claimed',?,?,'[\"private-image\"]')",
                   (time.time() - 100, time.time() - 1))
    scheduler = ReplyScheduler(config, Database(config.resolve(config.paths.database)),
                               SimpleNamespace(), SimpleNamespace())
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with queue.connect() as db:
                row = db.execute("SELECT status,prompt,images FROM jobs WHERE id='idle-stale'").fetchone()
            if row == ('expired', '', '[]'):
                break
            time.sleep(.02)
        assert row == ('expired', '', '[]')
    finally:
        scheduler.close()
        for thread in scheduler.threads:
            thread.join(3)
            assert not thread.is_alive()


def test_unclaimed_job_waits_longer_on_a_healthy_channel(tmp_path):
    import time
    from app.web_llm import WebReplyLLM
    queue=WebReplyLLM(AppConfig(project_root=tmp_path))
    started=time.time(); expires=started+150
    # No recent failure is recorded, so the channel gets the full window.
    assert queue._claim_grace_seconds('deepseek',started,expires)==75.0


def test_virtual_failure_is_ignored_and_only_same_provider_production_success_recovers(tmp_path):
    import time
    from app.db import Database
    from app.web_llm import WebReplyLLM

    config = AppConfig(project_root=tmp_path)
    queue = WebReplyLLM(config)
    state_db = Database(config.resolve(config.paths.database))
    started = time.time()
    expires = started + 150
    state_db.set_state('deepseek_last_failure',
                       {'code': 'known_conversation_missing', 'seen_at': started - 5, 'is_test': True})
    assert queue._claim_grace_seconds('deepseek', started, expires) == 75.0

    state_db.set_state('deepseek_last_failure',
                       {'code': 'page_failed', 'seen_at': started - 5, 'is_test': False})
    assert queue._claim_grace_seconds('deepseek', started, expires) == 15.0

    def complete(job_id, provider, is_test):
        with queue.connect() as db:
            db.execute('INSERT INTO jobs(id,prompt,status,created,expires,provider,is_test) '
                       'VALUES(?, ?, ?, ?, ?, ?, ?)',
                       (job_id, '', 'claimed', time.time(), time.time() + 60, provider, is_test))
        queue.complete(job_id, '{"action":"review","risk":"low","reply":"已完成"}')

    complete('virtual', 'deepseek', 1)
    assert queue._claim_grace_seconds('deepseek', started, expires) == 15.0
    complete('other-provider', 'chatgpt', 0)
    assert queue._claim_grace_seconds('deepseek', started, expires) == 15.0
    complete('production', 'deepseek', 0)
    assert queue._claim_grace_seconds('deepseek', started, expires) == 75.0
    assert state_db.get_state('deepseek_last_success')['is_test'] is False


@pytest.mark.parametrize(('code', 'shown'), [
    ('known_conversation_missing', True),
    ('private details 123', False),
])
def test_failed_job_reports_only_valid_fixed_failure_code(tmp_path, monkeypatch, code, shown):
    import json
    from app.web_llm import WebReplyLLM

    queue = WebReplyLLM(AppConfig(project_root=tmp_path,
                                  openai={'provider': 'deepseek_web', 'web_reply_timeout_seconds': 2}))

    def fail(_):
        with queue.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE status='pending'").fetchone()
            db.execute("UPDATE jobs SET status='failed',browser_meta=? WHERE id=?",
                       (json.dumps({'failure_code': code}), row[0]))

    monkeypatch.setattr('app.web_llm.time.sleep', fail)
    with pytest.raises(LLMError, match='网页回复失败') as caught:
        queue.decide('prompt', '{}', RiskLevel.low)
    assert (code in str(caught.value)) is shown
    assert caught.value.fallback_safe is False
