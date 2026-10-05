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
