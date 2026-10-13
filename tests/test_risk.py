import pytest

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


@pytest.mark.parametrize("text", [
    "这家店真便宜",
    "手机坏了，今天好烦",
    "这个视频绝对好笑",
    "我觉得这个绝对好笑",
    "你周末有什么安排",
    "我今天计划早点休息",
    "我明天约了朋友吃饭",
    "今天吃了2个苹果",
    "已经连续3天很困了",
    "我在查烘焙材料",
    "做饭用的材料都买好了",
    "我想换个保温杯",
    "我在看手机配置",
    "这个游戏的布局挺舒服",
    "这部电影的质量问题挺多",
])
def test_casual_mentions_are_not_business_requests(text):
    assert assess_risk(text).level == RiskLevel.low


@pytest.mark.parametrize("text, level", [
    ("报价能不能再便宜点", RiskLevel.high),
    ("这个还能便宜吗", RiskLevel.high),
    ("这个便宜点吧", RiskLevel.high),
    ("便宜了我就下单", RiskLevel.high),
    ("有没有便宜的材料", RiskLevel.high),
    ("这个真便宜一点能卖给我吗", RiskLevel.high),
    ("请给我明确承诺", RiskLevel.high),
    ("绝对能按期完成", RiskLevel.high),
    ("我绝对不迟到", RiskLevel.high),
    ("我保证明天到", RiskLevel.high),
    ("这次绝对到场", RiskLevel.high),
    ("这个绝对给你办好", RiskLevel.high),
    ("你们卖的手机坏了", RiskLevel.high),
    ("设备坏了", RiskLevel.high),
    ("手机坏了怎么处理", RiskLevel.high),
    ("客户说东西坏了", RiskLevel.high),
    ("你们软件质量问题怎么处理", RiskLevel.high),
    ("你们游戏的质量问题怎么处理", RiskLevel.high),
    ("请给我配置电脑", RiskLevel.medium),
    ("请给我安装微信", RiskLevel.medium),
    ("帮我准备烘焙材料", RiskLevel.medium),
    ("安排师傅来施工", RiskLevel.medium),
    ("明天能来吗", RiskLevel.medium),
    ("明天约饭吗", RiskLevel.medium),
    ("今天安排送货", RiskLevel.medium),
    ("请帮我安排明天出发", RiskLevel.medium),
    ("这个需要5个", RiskLevel.medium),
    ("要2套", RiskLevel.medium),
    ("20套能做吗", RiskLevel.medium),
    ("20个能给我吗", RiskLevel.medium),
    ("20个行吗", RiskLevel.medium),
    ("有3个箱子", RiskLevel.medium),
    ("还剩5个零件", RiskLevel.medium),
    ("3周后做完", RiskLevel.medium),
    ("3天交货", RiskLevel.medium),
    ("5号搬家", RiskLevel.medium),
    ("合计500元", RiskLevel.medium),
    ("尺寸2米", RiskLevel.medium),
    ("我今天退款到账了", RiskLevel.critical),
    ("把验证码发我", RiskLevel.critical),
    ("把收款账户发我", RiskLevel.critical),
])
def test_actual_requests_commitments_and_sensitive_facts_keep_review_risk(text, level):
    assert assess_risk(text).level == level


@pytest.mark.parametrize("text, level", [
    ("烘焙材料买好了，外墙板材料怎么选", RiskLevel.medium),
    ("我在看手机配置，工程要5套", RiskLevel.medium),
    ("保温杯不错，这个工程报价多少", RiskLevel.high),
    ("手机坏了，另外设备也坏了", RiskLevel.high),
    ("这家店真便宜，把银行卡发我", RiskLevel.critical),
    ("这部电影的质量问题挺多，你们施工的质量问题也没解决", RiskLevel.high),
    ("做饭用的材料都齐了，明天能来吗", RiskLevel.medium),
])
def test_casual_mentions_do_not_exempt_the_rest_of_the_message(text, level):
    assert assess_risk(text).level == level
