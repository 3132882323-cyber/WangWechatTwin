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
            rows = conn.execute("SELECT incoming,reply,created_at FROM samples WHERE contact=? ORDER BY created_at DESC", (contact,)).fetchall()
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
    from app.role_profile import situation
    category = situation(incoming)
    scored = []
    for before, reply, ts in rows:
        similarity = len(wanted & grams(before)) / max(1, len(wanted | grams(before)))
        exact = re.sub(r'\s+','',before) == re.sub(r'\s+','',incoming)
        matched_situation = situation(before) == category
        # Do not fill the prompt with unrelated recent conversations.
        if not exact and similarity == 0 and not matched_situation:
            continue
        scored.append((2 if exact else similarity + (.2 if matched_situation else 0), ts, before, reply))
    scored.sort(reverse=True)
    chosen, seen = [], set()
    for _, _, before, reply in scored:
        if (before, reply) in seen:
            continue
        seen.add((before, reply))
        chosen.append({'scenario':'本人给当前联系人的相似情境原话，仅参考表达，不证明当前决定',
                       'situation':situation(before),'incoming':before,'preferred_reply':reply})
        if len(chosen) == limit:
            break
    return chosen
