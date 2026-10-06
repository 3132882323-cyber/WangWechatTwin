"""Only narrow non-committal replies may bypass an unnecessary model review."""
import re
from app.risk import assess_risk
from app.models import RiskLevel


def safe_social_reply(text):
    if assess_risk(text).level!=RiskLevel.low or len(text)>24:return False
    plain=re.sub(r'[\s。.!！?？,，]+','',text)
    return bool(re.fullmatch(r'(?:好{1,3}|OK|ok|行|对|嗯{1,3}|哦|哈哈{0,5}|咋了|啥事|什么愿望|啥愿望|啥意思|什么意思|你说|你说说|怎么了|真的假的|咋真想我了|你真想我了|\[捂脸\]|\[呲牙\]|\[笑哭\])',plain))
