"""Proactive suggestions are always drafts; never sends from the scheduler."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import time
import re

from app.models import ReplyDecision, RiskLevel
from app.risk import assess_risk


def candidate_topics(rows):
    """Only current contact's recent low-risk original words may form a question."""
    selected=[]
    for row in rows:
        text=str(row['content']).strip()
        if row['direction']!='in' or assess_risk(text).level!=RiskLevel.low:
            continue
        if not 6<=len(text)<=36 or re.search(r'https?://|\d{7,}|\[|<',text):
            continue
        if re.search(r'报价|付款|合同|交货|工期|账号|验证码|密码|退款|赔偿',text):
            continue
        if re.search(r'怎么样|咋样|准备|打算|最近|工作|比赛|考试|学习|去玩|旅游|忙',text):
            selected.append(text)
    return selected[:2]


class ProactivePlanner:
    def __init__(self, config, db, adapter):
        self.config,self.db,self.adapter=config,db,adapter
        self.last_tick=0

    def eligible(self, now=None):
        now=now or datetime.now(timezone.utc)
        cfg=self.config.proactive
        local=now.astimezone(timezone(timedelta(hours=8)))
        if not cfg.enabled or not cfg.active_start_hour<=local.hour<cfg.active_end_hour:
            return []
        reader=getattr(self.adapter,'reader',None)
        path=self.config.resolve(self.config.paths.chat_memory)
        if not reader or not path.exists():return []
        state=self.db.get_state('proactive_planner') or {}
        day=local.date().isoformat()
        generated=state.get('daily_count',0) if state.get('day')==day else 0
        if generated>=cfg.max_drafts_per_day:return []
        allow=set(reader.existing_conversations) if cfg.all_existing_contacts else set(cfg.contacts)
        output=[]
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
            connection.row_factory=sqlite3.Row
            latest=connection.execute('SELECT contact,MAX(created_at) AS last_at FROM history GROUP BY contact ORDER BY last_at DESC').fetchall()
            for item in latest:
                contact=item['contact'];last=int(item['last_at'])
                if contact not in allow or contact==reader.self_username or contact.endswith('@chatroom') or contact.startswith('gh_'):
                    continue
                if contact in {'filehelper','weixin','qqmail','fmessage','newsapp','medianote','notification_messages'}:
                    continue
                if now.timestamp()-last<cfg.min_idle_hours*3600 or now.timestamp()-last>30*86400:continue
                if now.timestamp()-state.get('contacts',{}).get(contact,0)<cfg.cooldown_hours*3600:continue
                with self.db.connect() as draft_db:
                    pending=draft_db.execute("SELECT 1 FROM drafts WHERE contact=? AND status IN ('pending','approved') LIMIT 1",(contact,)).fetchone()
                if pending:continue
                rows=connection.execute('SELECT direction,content,created_at FROM history WHERE contact=? ORDER BY created_at DESC LIMIT 12',(contact,)).fetchall()
                if not rows or rows[0]['direction']=='in':continue  # A normal reply is still owed.
                # Do not nudge a person repeatedly after two unanswered owner messages.
                if len(rows)>1 and rows[0]['direction']=='out' and rows[1]['direction']=='out':continue
                output.append((contact,last,rows))
                if len(output)>=cfg.max_drafts_per_day-generated:break
        return output

    def tick(self, *, force=False, now=None):
        if self.config.resolve(self.config.paths.pause_file).exists():return []
        clock=time.monotonic()
        if not force and clock-self.last_tick<self.config.proactive.interval_seconds:return []
        self.last_tick=clock;now=now or datetime.now(timezone.utc)
        generated=[]
        for contact,last,rows in self.eligible(now):
            topics=candidate_topics(rows) if self.config.proactive.purpose!='chat' else []
            if topics:
                reply='上次你说“'+topics[0]+'”，后来咋样了'
                reason='主动跟进建议：引用对方近期原话，只询问进展；历史内容不作为当前事实'
            elif self.config.proactive.purpose=='followup':
                continue
            else:
                reply='最近咋样'
                reason='主动闲聊建议：中性问候，不编造本人行程或对方状态'
            decision=ReplyDecision(action='review',risk=RiskLevel.low,reply=reply,confidence=.85,
                                   reason=reason+'；仅生成草稿，批准后才会联系对方')
            draft=self.db.create_draft(contact,None,decision,kind='proactive',source_context_ts=last)
            self.db.add_event('proactive_draft',reason,contact=contact)
            state=self.db.get_state('proactive_planner') or {};day=now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
            count=state.get('daily_count',0) if state.get('day')==day else 0
            state.update(day=day,daily_count=count+1);state.setdefault('contacts',{})[contact]=now.timestamp()
            self.db.set_state('proactive_planner',state);generated.append(draft)
        return generated
