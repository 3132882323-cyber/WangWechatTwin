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
        connection.executemany('INSERT OR REPLACE INTO history VALUES(?,?,?,?,?,?)',
                               [(identity, contact, direction, timestamp, text, assess_risk(text).level.value)
                                for identity, contact, direction, timestamp, text in rows])
        connection.commit()
    finally:
        connection.close()


def retrieve(path, contact, query, limit=12, max_chars=6000):
    if not path.exists():
        return []
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id,direction,created_at,content,risk FROM history WHERE contact=? AND risk!='critical' ORDER BY created_at DESC LIMIT 2500", (contact,)).fetchall()
    finally:
        conn.close()
    rows = [r for r in rows if assess_risk(r[3]).level != RiskLevel.critical]
    recent = rows[:min(6, limit)]
    wanted = grams(query)
    scored = sorted(((len(wanted & grams(r[3])) / max(1, len(wanted | grams(r[3]))), r[2], r) for r in rows), reverse=True)
    selected = {r[0]: r for r in recent}
    for score, _, row in scored:
        if len(selected) >= limit:
            break
        if score > 0:
            selected[row[0]] = row
    output, used = [], 0
    for _, direction, ts, content, risk in sorted(selected.values(), key=lambda r: r[2]):
        content = content[:1200]
        if used + len(content) > max_chars:
            continue
        used += len(content)
        output.append({"speaker": "本人" if direction == "out" else "对方", "time": datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).isoformat(),
                       "content": content, "historical_risk": risk, "source": "当前联系人的本机历史记录；不是当前事实确认"})
    return output
