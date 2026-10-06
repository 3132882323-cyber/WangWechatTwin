"""A factual-free greeting can reuse this contact's verified owner wording."""
import re,sqlite3,json
from app.risk import assess_risk
from app.models import RiskLevel


def lookup(path,contact,incoming,profile_path=None):
    normalize=lambda s:re.sub(r'[\s。.!！?？,，]','',s)
    query=normalize(incoming)
    if query not in {'在吗','在不','你好','您好'}:return None
    rows=[]
    if path.exists():
        connection=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:rows=connection.execute('SELECT incoming,reply FROM samples WHERE contact=? ORDER BY created_at DESC',(contact,)).fetchall()
        finally:connection.close()
    allowed={'咋了','你好','您好','在','我在','你说','说吧','在说吧','什么事','啥事','怎么了','在呢'}
    for before,reply in rows:
        if normalize(before)==query and normalize(reply) in allowed and assess_risk(reply).level==RiskLevel.low:
            return reply
    # Only a closed, factual-free opener statistic may generalize. No other
    # contact's message, story or decision is read into this conversation.
    if query in {'在吗','在不'} and profile_path:
        try:global_habits=json.loads(profile_path.read_text(encoding='utf-8')).get('global',{})
        except (OSError,ValueError):return None
        openers=global_habits.get('common_conversation_openers',[])
        total=sum(n for _,n in openers)
        for wording,count in openers:
            if wording in {'咋了','什么事','啥事','你说','说吧','怎么了'} and count>=20 and count/max(1,total)>=.6:
                return wording
    return None
