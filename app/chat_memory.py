"""Dated, local conversation retrieval with strict contact isolation."""
import sqlite3
from datetime import datetime, timezone, timedelta

from app.style_history import grams
from app.risk import assess_risk
from app.models import RiskLevel


def remember(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY,contact TEXT,direction TEXT,created_at INTEGER,content TEXT,risk TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS history_contact_time ON history(contact,created_at DESC)')
        if 'speaker' not in {r[1] for r in connection.execute('PRAGMA table_info(history)')}:
            connection.execute("ALTER TABLE history ADD COLUMN speaker TEXT NOT NULL DEFAULT ''")
        values=[]
        for identity,contact,direction,timestamp,text,*extra in rows:
            speaker=extra[0] if extra else ''
            values.append((identity,contact,direction,timestamp,text,assess_risk(text).level.value,speaker))
        connection.executemany('INSERT OR REPLACE INTO history(id,contact,direction,created_at,content,risk,speaker) VALUES(?,?,?,?,?,?,?)',values)
        connection.commit()
    finally:
        connection.close()


def retrieve(path, contact, query, limit=18, max_chars=7000,as_of=None):
    if not path.exists():
        return []
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        speaker_column='speaker' if 'speaker' in {r[1] for r in conn.execute('PRAGMA table_info(history)')} else "''"
        rows = conn.execute(f"SELECT id,direction,created_at,content,risk,{speaker_column} FROM history WHERE contact=? AND risk!='critical' AND (? IS NULL OR created_at<=?) ORDER BY created_at DESC LIMIT 25000", (contact,as_of,as_of)).fetchall()
    finally:
        conn.close()
    rows = [r for r in rows if assess_risk(r[3]).level != RiskLevel.critical]
    recent = rows[:min(6, limit)]
    wanted = grams(query)
    scored = sorted(((len(wanted & grams(r[3])) / max(1, len(wanted | grams(r[3]))), r[2], r) for r in rows), reverse=True)
    selected = {r[0]: r for r in recent}
    chronological=sorted(rows,key=lambda r:r[2]);positions={r[0]:i for i,r in enumerate(chronological)}
    for score, _, row in scored:
        if len(selected) >= limit:
            break
        if score > 0:
            index=positions[row[0]]
            # Recall the exchange around the event, not an isolated sentence
            # whose first-person pronoun can be attributed to the wrong person.
            for neighbor in chronological[max(0,index-1):index+3]:
                if len(selected)<limit:selected[neighbor[0]]=neighbor
    output, used = [], 0
    for record_id, direction, ts, content, risk,speaker in sorted(selected.values(), key=lambda r: r[2]):
        content = content[:1200]
        if used + len(content) > max_chars:
            continue
        used += len(content)
        label=(speaker if speaker.startswith('本人（') else '本人') if direction=='out' else (speaker or ('群成员（未确认）' if contact.endswith('@chatroom') else '对方'))
        output.append({"speaker": label, "time": datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).isoformat(),
                       "content": content, 'evidence_id':record_id[0:16], 'speaker_rule':'第一人称归属该条说话人；转述中的人不等同本人',"historical_risk": risk, "source": "当前联系人的本机历史记录；不是当前事实确认"})
    return output
