"""Local, per-contact writing samples; no cross-contact history is returned."""
from __future__ import annotations

import json
import re
import sqlite3


def grams(text):
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i+2] for i in range(max(1, len(text)-1))} if text else set()


def examples(path, contact, incoming, limit=4):
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute("SELECT incoming,reply,created_at FROM samples WHERE contact=? ORDER BY created_at DESC LIMIT 300", (contact,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    wanted = grams(incoming)
    from app.risk import assess_risk
    from app.models import RiskLevel
    rows = [(before, reply, ts) for before, reply, ts in rows
            if assess_risk(before).level == RiskLevel.low and assess_risk(reply).level == RiskLevel.low
            and not re.search(r"https?://|\d{7,}", before + reply)]
    scored = [(len(wanted & grams(before)) / max(1, len(wanted | grams(before))), ts, before, reply)
              for before, reply, ts in rows]
    scored.sort(reverse=True)
    return [{"scenario": "本人给当前联系人的历史回复，只用于表达风格", "incoming": before,
             "preferred_reply": reply} for _, _, before, reply in scored[:limit]]
