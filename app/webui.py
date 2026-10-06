from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone, timedelta
import threading
import time
import asyncio
from urllib.parse import parse_qs
from pathlib import Path
import re

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse

from app.config import AppConfig
from app.db import Database


def _page(config: AppConfig, db: Database, csrf_token: str = "") -> str:
    try:
        labels = json.loads((config.resolve(config.paths.history_reader) / "contact_labels.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        labels = {}
    paused = config.resolve(config.paths.pause_file).exists()
    drafts = db.list_drafts(status="pending", limit=60)
    events = db.recent_events(limit=60)
    rows = []
    test_labels={'__web_self_test__voice':'语音识别自检（测试草稿，不发送）',
                 '__web_self_test__image':'图片识别自检（测试草稿，不发送）',
                 '__web_self_test__sticker_recognition':'表情识别自检（测试草稿，不发送）',
                 '__web_self_test__sticker':'表情图片自检（测试草稿，不发送）'}
    draft_only = config.mode in {"shadow", "off"}
    for draft in drafts:
        contact_label=test_labels.get(draft.contact,'网页连接自检（测试草稿，不发送）' if draft.contact.startswith('__web_self_test__') else labels.get(draft.contact,draft.contact))
        allowed = (not draft.contact.startswith('__web_self_test__') and not draft_only and (config.adapter != "history_verified_sender"
                   or config.wechat.sender_all_existing_chats or draft.contact in config.wechat.sender_allowed_contacts))
        approve_action = "approve" if allowed else "save"
        approve_label = ("批准这次主动联系" if draft.kind=='proactive' else "批准发送并学习我的修改") if allowed else "保存我的修改"
        if draft.kind=='sticker' and allowed:approve_label='批准发送这张表情图片'
        text = html.escape(draft.edited_reply or draft.reply)
        incoming = html.escape("主动联系建议：尚未给对方发消息" if draft.kind=='proactive' else draft.incoming_content or "（原消息已按保留策略清理）")
        previews=[]
        for raw_path in draft.incoming_media_paths:
            path=Path(raw_path).resolve()
            for kind in ['image_assets','sticker_assets']:
                if path.parent==(config.resolve(config.paths.history_reader)/kind).resolve():
                    previews.append(f'<img alt="本条消息的图片预览" style="max-width:240px;max-height:280px" src="/media-preview/{kind}/{html.escape(path.name)}">')
        description=f'<div class="incoming"><strong>识别内容：</strong>{html.escape(draft.media_description)}</div>' if draft.media_description else ''
        source_label='<div class="reason">语音自动听写：请核对含糊词、人名和数字。</div>' if draft.original_type=='voice' else ''
        rows.append(
            f"""
            <section class="card">
              <div class="meta">#{draft.id} · {'主动聊天建议 · ' if draft.kind=='proactive' else '表情图片建议 · ' if draft.kind=='sticker' else ''}{html.escape(contact_label)} · 风险 {html.escape(draft.risk)} · 置信度 {draft.confidence:.2f}</div>
              <div class="incoming"><strong>对方：</strong>{incoming}</div>
              {source_label}{description}<div class="media-previews">{''.join(previews)}</div>
              <div class="reason"><strong>系统判断：</strong>{html.escape(draft.reason)}</div>
              {'<img alt="拟发送的表情图片" style="max-width:240px;max-height:240px" src="/sticker-preview/'+html.escape(draft.sticker_id)+'">' if draft.kind=='sticker' else ''}
              <form method="post" action="/draft/{draft.id}/{approve_action}">
                <input type="hidden" name="_csrf" value="{csrf_token}">
                <label for="draft-reply-{draft.id}">拟发送内容</label>
                <textarea id="draft-reply-{draft.id}" name="reply" {'readonly' if draft.kind=='sticker' else ''}>{text}</textarea>
                <div class="actions">
                  <button class="send" type="submit">{approve_label}</button>
                  <button class="dismiss" type="submit" formaction="/draft/{draft.id}/dismiss">不发送</button>
                </div>
              </form>
            </section>
            """
        )
    titles = {"runtime_started": "微信后台已启动", "runtime_stopped": "微信后台已停止", "auto_sent": "已自动回复",
              "proactive_draft":"主动聊天建议待审核",
              "sticker_draft":"表情图片建议待审核",
              "approved_sent": "已发送你确认的回复", "draft_created": "等待你确认", "media_review": "非文字消息待查看",
              "ignored_acknowledgment": "已识别简短确认，无需追加回复", "ignored": "无需回复", "send_error": "发送受阻",
              "paused": "已暂停", "resumed": "已恢复", "draft_saved": "修改已保存", "llm_error": "AI 连接受阻"}
    rendered_events = []
    for event in events:
        try:
            when = datetime.fromisoformat(str(event["created_at"])).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
        except ValueError:
            when = str(event["created_at"])
        title = titles.get(event["event_type"], event["event_type"])
        contact = labels.get(event.get("contact"), event.get("contact") or "")
        detail = "日常文字自动回复，重要事项留草稿" if event["event_type"] == "runtime_started" and not draft_only else "只生成草稿，不发送" if event["event_type"] == "runtime_started" else event["detail"]
        rendered_events.append(f"<tr><td>{html.escape(when)}</td><td>{html.escape(str(title))}</td><td>{html.escape(str(contact))}</td><td>{html.escape(str(detail))}</td></tr>")
    event_rows = "".join(rendered_events)
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信聊天分身</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6f8;margin:0;color:#202124}}
header{{background:#111827;color:white;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}}
main{{max-width:1100px;margin:24px auto;padding:0 18px}}
.badge{{padding:6px 10px;border-radius:999px;background:{'#b91c1c' if paused else '#047857'}}}
.card{{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 2px 8px #0000000a}}
.meta{{font-weight:700;margin-bottom:8px}} .incoming{{background:#f3f4f6;padding:10px;border-radius:8px;margin-bottom:8px;white-space:pre-wrap}} .reason{{color:#6b7280;margin-bottom:10px}}
.media-previews{{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}}
textarea{{width:100%;min-height:90px;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;padding:10px;font-size:16px}}
button:focus-visible,textarea:focus-visible{{outline:3px solid #2563eb;outline-offset:3px}} label{{display:block;margin:8px 0;font-weight:600}}
.actions{{display:flex;gap:10px;margin-top:10px}} button{{border:0;border-radius:8px;padding:10px 16px;cursor:pointer}}
.send{{background:#047857;color:white}} .dismiss{{background:#e5e7eb}} .pause{{background:#b91c1c;color:white}}
table{{width:100%;border-collapse:collapse;background:white}} th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
small{{color:#9ca3af}}
</style></head>
<body>
<header><div><strong>微信聊天分身</strong><br><small>{'当前只生成草稿，不会发送微信消息' if draft_only else '已接管已有聊天：日常文字自动回复，重要事项待审核；群聊只处理@你的消息' if config.wechat.sender_all_existing_chats else '仅接管已确认的测试联系人，其他联系人只生成草稿' if config.adapter == 'history_verified_sender' else '已开启发送功能'}</small></div>
<div><span class="badge">{'已暂停' if paused else '运行中'}</span></div></header>
<main>
<div class="card">{'普通 ChatGPT 网页：不个性化临时会话，只输入微信上下文；连接失败不会自动切回 Codex。' if config.openai.provider == 'web' else '使用当前配置的模型连接。'}</div>
<form method="post" action="/{'resume' if paused else 'pause'}"><input type="hidden" name="_csrf" value="{csrf_token}"><button class="pause">{'恢复处理' if paused else '立即暂停'}</button></form>
<section class="card"><strong>主动聊天：{'已开启建议' if config.proactive.enabled else '未开启'}</strong><p>问候与话题跟进均先生成草稿，你批准后才联系对方。每天最多 {config.proactive.max_drafts_per_day} 条；同一联系人至少间隔 {config.proactive.cooldown_hours:g} 小时；北京时间 {config.proactive.active_start_hour}:00—{config.proactive.active_end_hour}:00 生成建议。已有未回复消息时不再催聊。</p></section>
<section class="card"><strong>表情图片：{'已启用审核建议' if config.stickers.enabled else '未启用'}</strong><p>对方明确分享好消息时，建议使用已批准的表情图片。预览后批准才发送；通过本机接口发送原图，每个联系人每天最多 {config.stickers.max_per_contact_per_day} 条建议。</p></section>
<section class="card"><strong>语音、图片和表情识别</strong><p>语音在本机转文字：{'已启用' if config.media.voice_enabled else '未启用'}，处理中 {(db.get_state('media_worker_voice') or {}).get('pending_count',0)} 条。图片和表情取图：{'已启用' if config.media.images_enabled else '未启用'}，处理中 {(db.get_state('media_worker_visual') or {}).get('pending_count',0)} 条。普通图片回复先审核；识别不清楚的表情和语音不会自动发送。</p></section>
<h2>待审核草稿（{len(drafts)}）</h2>
{''.join(rows) if rows else '<div class="card">当前没有待审核草稿。</div>'}
<h2>最近事件</h2>
<table><thead><tr><th>时间</th><th>事件</th><th>联系人</th><th>说明</th></tr></thead><tbody>{event_rows}</tbody></table>
</main>
<script>
setInterval(function(){{
  if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') return;
  window.location.reload();
}}, 8000);
</script>
</body></html>
"""


def create_app(config: AppConfig, db: Database) -> FastAPI:
    app = FastAPI(title="微信聊天分身")
    app.state.csrf_token = secrets.token_urlsafe(32)
    if config.openai.provider == 'web':
        from app.web_llm import WebReplyLLM
        queue = WebReplyLLM(config)
        token_path = config.resolve(config.paths.browser_bridge) / 'pairing_token.txt'
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
        bridge_token = token_path.read_text(encoding='utf-8').strip()

        @app.websocket('/browser-bridge/events')
        async def browser_events(socket: WebSocket):
            await socket.accept()
            try:
                auth=await asyncio.wait_for(socket.receive_json(),timeout=5)
                if not secrets.compare_digest(str(auth.get('token','')),bridge_token):
                    await socket.close(code=1008);return
                pulse=0;notified=0
                while True:
                    now=time.time();paused=config.resolve(config.paths.pause_file).exists()
                    with queue.connect() as conn:
                        pending=conn.execute("SELECT 1 FROM jobs WHERE status='pending' AND expires>? AND (?=0 OR is_test=1) LIMIT 1",(now,int(paused))).fetchone()
                    if pending and now-notified>=.5:
                        await socket.send_json({'type':'ready'});notified=now;pulse=now
                    elif now-pulse>=20:
                        await socket.send_json({'type':'heartbeat'});pulse=now
                    await asyncio.sleep(.25)
            except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
                return

        def bridge_auth(request):
            supplied = request.headers.get('authorization', '')
            if not secrets.compare_digest(supplied, 'Bearer ' + bridge_token):
                raise HTTPException(403, '网页连接未配对')

        @app.get('/browser-bridge/next')
        async def browser_next(request: Request):
            bridge_auth(request)
            if request.headers.get('x-wechat-bridge-version') == '4':
                db.set_state('browser_bridge', {'version':4,'seen_at':time.time(),'max_owned_tabs':3,'build':request.headers.get('x-wechat-bridge-build','original')})
            paused = config.resolve(config.paths.pause_file).exists()
            with queue.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute("SELECT id,prompt,conversation_key,images,created FROM jobs WHERE status='pending' AND expires>? AND (?=0 OR is_test=1) ORDER BY is_test ASC,created LIMIT 1", (time.time(), int(paused))).fetchone()
                if row:
                    conn.execute("UPDATE jobs SET status='claimed' WHERE id=?", (row[0],))
            return {'job': {'id': row[0], 'prompt': row[1], 'conversation_key':row[2] or 'isolated:'+row[0],'images':json.loads(row[3]),'created':row[4]} if row else None}

        @app.post('/browser-bridge/result')
        async def browser_result(request: Request):
            bridge_auth(request)
            body = await request.json()
            with queue.connect() as conn:
                test_row = conn.execute('SELECT is_test FROM jobs WHERE id=?', (body.get('id',''),)).fetchone()
            if config.resolve(config.paths.pause_file).exists() and not (test_row and test_row[0]):
                raise HTTPException(409, '已暂停')
            if body.get('error'):
                with queue.connect() as conn:
                    changed=conn.execute("UPDATE jobs SET status='failed',prompt='' WHERE id=? AND status='claimed'", (body['id'],)).rowcount
                if changed != 1:
                    raise HTTPException(409,'任务已过期或已处理')
                if test_row and test_row[0]:
                    db.add_event('browser_test_error','独立浏览器验证失败；未暂停生产回复')
                else:
                    from app.browser_health import failed
                    failed(config,db,body.get('error'))
                return {'ok': True}
            try:
                queue.complete(body['id'], body['result'],body.get('browser_meta'))
            except (ValueError, KeyError):
                raise HTTPException(409, '任务或结果无效')
            if not (test_row and test_row[0]):
                from app.browser_health import succeeded
                succeeded(db)
            return {'ok': True}

    async def owner_action(request: Request) -> None:
        body = (await request.body()).decode("utf-8", errors="replace")
        token = parse_qs(body).get("_csrf", [""])[0]
        if not secrets.compare_digest(token, app.state.csrf_token):
            raise HTTPException(status_code=403, detail="请从本机审核台执行操作")

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return _page(config, db, app.state.csrf_token)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "paused": config.resolve(config.paths.pause_file).exists()})

    @app.get('/sticker-preview/{digest}')
    async def sticker_preview(digest:str):
        try:
            from app.sticker_catalog import item
            entry=item(config,digest)
            return FileResponse(entry['preview_path'],media_type='image/png',headers={'Cache-Control':'no-store'})
        except (OSError,ValueError,KeyError,TypeError):
            raise HTTPException(status_code=404,detail='表情图片未批准或校验失败') from None

    @app.get('/media-preview/{kind}/{filename}')
    async def media_preview(kind:str,filename:str):
        if kind not in {'image_assets','sticker_assets'} or not re.fullmatch(r'[a-f0-9]{32,64}(?:-\d{1,3})?\.png',filename):
            raise HTTPException(404,'图片预览不存在')
        root=(config.resolve(config.paths.history_reader)/kind).resolve();path=(root/filename).resolve()
        if path.parent!=root or not path.is_file() or path.stat().st_size>2*1024*1024:
            raise HTTPException(404,'图片预览不存在')
        with path.open('rb') as source:
            if source.read(8)!=b'\x89PNG\r\n\x1a\n':raise HTTPException(404,'图片预览无效')
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.post("/pause")
    async def pause(request: Request) -> RedirectResponse:
        await owner_action(request)
        config.resolve(config.paths.pause_file).write_text("paused", encoding="utf-8")
        db.add_event("paused", "通过本地控制台暂停")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/resume")
    async def resume(request: Request) -> RedirectResponse:
        await owner_action(request)
        config.resolve(config.paths.pause_file).unlink(missing_ok=True)
        db.add_event("resumed", "通过本地控制台恢复")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/draft/{draft_id}/approve")
    async def approve(draft_id: int, request: Request) -> RedirectResponse:
        await owner_action(request)
        if config.mode in {"shadow", "off"}:
            raise HTTPException(status_code=403, detail="当前只生成草稿，未开启发送")
        if config.adapter == "history_verified_sender":
            draft = next((d for d in db.list_drafts(status="pending", limit=1000) if d.id == draft_id), None)
            if draft is None or (not config.wechat.sender_all_existing_chats and draft.contact not in config.wechat.sender_allowed_contacts):
                raise HTTPException(status_code=403, detail="该联系人未授权发送")
            if draft.contact.startswith('__web_self_test__'):
                raise HTTPException(status_code=403, detail='本机网页自检草稿不能发送')
        raw = (await request.body()).decode("utf-8", errors="replace")
        reply = parse_qs(raw).get("reply", [""])[0].strip()
        if reply:
            db.update_draft(draft_id, "approved", reply)
            db.add_event("draft_approved", f"草稿 #{draft_id} 已批准")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/draft/{draft_id}/save")
    async def save(draft_id: int, request: Request) -> RedirectResponse:
        await owner_action(request)
        raw = (await request.body()).decode("utf-8", errors="replace")
        reply = parse_qs(raw).get("reply", [""])[0].strip()
        pending = {draft.id for draft in db.list_drafts(status="pending", limit=1000)}
        if draft_id not in pending:
            raise HTTPException(status_code=409, detail="该草稿已处理，请刷新页面")
        if reply:
            db.update_draft(draft_id, "pending", reply)
            db.add_event("draft_saved", f"草稿 #{draft_id} 修改已保存，未批准发送")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/draft/{draft_id}/dismiss")
    async def dismiss(draft_id: int, request: Request) -> RedirectResponse:
        await owner_action(request)
        db.update_draft(draft_id, "dismissed")
        db.add_event("draft_dismissed", f"草稿 #{draft_id} 已取消")
        return RedirectResponse(url="/", status_code=303)

    return app


def start_webui(config: AppConfig, db: Database) -> threading.Thread | None:
    if not config.web.enabled:
        return None
    app = create_app(config, db)

    def runner() -> None:
        uvicorn.run(
            app,
            host=config.web.host,
            port=config.web.port,
            log_level="warning",
            access_log=False,
        )

    thread = threading.Thread(target=runner, daemon=True, name="webui")
    thread.start()
    return thread
