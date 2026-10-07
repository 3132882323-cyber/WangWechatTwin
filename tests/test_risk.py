from app.models import RiskLevel
from app.risk import assess_risk


def test_low_risk():
    assert assess_risk("在吗").level == RiskLevel.low


def test_high_price():
    assert assess_risk("这个报价还能便宜吗").level == RiskLevel.high


def test_critical_bank():
    assert assess_risk("把收款账户发我").level == RiskLevel.critical


def test_medium_material():
    assert assess_risk("外墙板材料怎么选").level == RiskLevel.medium

def test_dates_in_casual_feelings_do_not_create_commitments():
    assert assess_risk('我今天有点累，想早点休息').level == RiskLevel.low
    assert assess_risk('今天心情挺好').level == RiskLevel.low
    assert assess_risk('明天能来吗').level == RiskLevel.medium
    assert assess_risk('今天送货').level != RiskLevel.low
    assert assess_risk('今天报价多少钱').level == RiskLevel.high
