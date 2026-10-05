from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone, timedelta
import threading
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
    draft_only = config.mode in {"shadow", "off"}
    for draft in drafts:
        allowed = (not draft_only and (config.adapter != "history_verified_sender"
                   or config.wechat.sender_all_existing_chats or draft.contact in config.wechat.sender_allowed_contacts))
        approve_action = "approve" if allowed else "save"
        approve_label = "批准发送并学习我的修改" if allowed else "保存我的修改"
        text = html.escape(draft.edited_reply or draft.reply)
        incoming = html.escape(draft.incoming_content or "（原消息已按保留策略清理）")
        rows.append(
            f"""
            <section class="card">
              <div class="meta">#{draft.id} · {html.escape(labels.get(draft.contact, draft.contact))} · 风险 {html.escape(draft.risk)} · 置信度 {draft.confidence:.2f}</div>
              <div class="incoming"><strong>对方：</strong>{incoming}</div>
              <div class="reason"><strong>系统判断：</strong>{html.escape(draft.reason)}</div>
              <form method="post" action="/draft/{draft.id}/{approve_action}">
                <input type="hidden" name="_csrf" value="{csrf_token}">
                <textarea name="reply">{text}</textarea>
                <div class="actions">
                  <button class="send" type="submit">{approve_label}</button>
                  <button class="dismiss" type="submit" formaction="/draft/{draft.id}/dismiss">不发送</button>
                </div>
              </form>
            </section>
            """
        )
    titles = {"runtime_started": "微信后台已启动", "runtime_stopped": "微信后台已停止", "auto_sent": "已自动回复",
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
<title>王总微信数字分身</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f5f6f8;margin:0;color:#202124}}
header{{background:#111827;color:white;padding:18px 26px;display:flex;justify-content:space-between;align-items:center}}
main{{max-width:1100px;margin:24px auto;padding:0 18px}}
.badge{{padding:6px 10px;border-radius:999px;background:{'#b91c1c' if paused else '#047857'}}}
.card{{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 2px 8px #0000000a}}
.meta{{font-weight:700;margin-bottom:8px}} .incoming{{background:#f3f4f6;padding:10px;border-radius:8px;margin-bottom:8px;white-space:pre-wrap}} .reason{{color:#6b7280;margin-bottom:10px}}
textarea{{width:100%;min-height:90px;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;padding:10px;font-size:16px}}
.actions{{display:flex;gap:10px;margin-top:10px}} button{{border:0;border-radius:8px;padding:10px 16px;cursor:pointer}}
.send{{background:#047857;color:white}} .dismiss{{background:#e5e7eb}} .pause{{background:#b91c1c;color:white}}
table{{width:100%;border-collapse:collapse;background:white}} th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
small{{color:#9ca3af}}
</style></head>
<body>
<header><div><strong>王总微信数字分身</strong><br><small>{'当前只生成草稿，不会发送微信消息' if draft_only else '已接管已有聊天：日常文字自动回复，重要事项待审核；群聊只处理@你的消息' if config.wechat.sender_all_existing_chats else '仅接管已确认的测试联系人，其他联系人只生成草稿' if config.adapter == 'history_verified_sender' else '已开启发送功能'}</small></div>
<div><span class="badge">{'已暂停' if paused else '运行中'}</span></div></header>
<main>
<form method="post" action="/{'resume' if paused else 'pause'}"><input type="hidden" name="_csrf" value="{csrf_token}"><button class="pause">{'恢复处理' if paused else '立即暂停'}</button></form>
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
    app = FastAPI(title="王总微信数字分身")
    app.state.csrf_token = secrets.token_urlsafe(32)

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
