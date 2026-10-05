"""Build private communication statistics from a locally verified owner history."""
from pathlib import Path
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import argparse
import json
import re
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import load_config
from app.role_profile import summarize, situation
from app.risk import assess_risk


def build(config, history):
    owner = defaultdict(list)
    with closing(sqlite3.connect(history.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as src:
        for peer,text in src.execute('SELECT conversation,content FROM messages WHERE from_me=1 AND (local_type&4294967295)=1'):
            if not isinstance(text,str) or not text.strip() or len(text)>300 or '<' in text or re.search(r'https?://|\d{7,}',text):
                continue
            if assess_risk(text).level.value == 'critical':
                continue
            owner[peer].append(text)
    paired = defaultdict(lambda:defaultdict(list))
    samples=config.resolve(config.paths.style_history)
    if samples.exists():
        with closing(sqlite3.connect(samples.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            for peer,before,reply in db.execute('SELECT contact,incoming,reply FROM samples'):
                paired[peer][situation(before)].append(reply)
    profile={'version':1,'created_at':datetime.now(timezone.utc).isoformat(),
             'source':'本人已核验微信文字；只有表达统计，不含跨联系人原文或个人事实',
             'global':summarize([t for rows in owner.values() for t in rows]),'contacts':{}}
    for peer,rows in owner.items():
        profile['contacts'][peer]={'style':summarize(rows),'situations':{kind:summarize(replies) for kind,replies in paired[peer].items()}}
    target=config.resolve(config.paths.role_profile);target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.build.json');temp.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(target)
    return {'owner_texts_analyzed':profile['global']['sample_count'],'contact_profiles':len(owner),
            'contact_situation_profiles':sum(len(v) for v in paired.values()),'raw_history_uploaded':False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='从已核验本机历史提炼本人表达习惯；不使用 AI 输出作为本人原话')
    parser.add_argument('--config',default='config.yaml')
    parser.add_argument('--history',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(load_config(args.config),args.history),ensure_ascii=False))
