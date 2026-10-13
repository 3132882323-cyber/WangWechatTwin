from __future__ import annotations

import re

from app.models import RiskAssessment, RiskLevel


CRITICAL_PATTERNS: dict[str, str] = {
    r"api[_ -]?key|access[_ -]?token|secret[_ -]?key|sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----": "涉及接口密钥或访问凭证",
    r"验证码|短信码|登录码|密码|口令": "涉及账号凭证",
    r"银行卡|银行账户|收款账户|收款码|转账|汇款|打款": "涉及资金账户或转账",
    r"身份证|营业执照原件|公章|合同盖章|签字确认": "涉及身份、印章或正式签署",
    r"起诉|律师函|报警|刑事|行政处罚|仲裁": "涉及法律或执法事项",
    r"赔偿|退款|退全款|违约金|承担责任": "涉及赔偿、退款或责任确认",
}

HIGH_PATTERNS: dict[str, str] = {
    r"报价|价格|多少钱|单价|总价|预算|优惠|便宜": "涉及价格或报价",
    r"工期|交期|什么时候完工|什么时候做好|什么时候到货|几天能做完": "涉及工期或交付承诺",
    r"合同|协议|定金|尾款|欠款|工程款|付款|发票|税点": "涉及合同、款项或票税",
    r"保证|承诺|绝对|肯定没问题": "要求明确保证或承诺",
    r"投诉|质量问题|坏了|漏水|返工|售后|退货|换货": "涉及投诉、质量或售后",
    r"云筑|中建|国资委|黑名单|风控": "涉及重点平台、欠款或风控事项",
}

MEDIUM_PATTERNS: dict[str, str] = {
    r"材料|板材|保温|门窗|地面|电路|配置": "涉及材料或配置",
    r"尺寸|图纸|方案|布局|效果图|施工": "涉及方案、尺寸或施工",
    r"库存|现货|运输|物流|吊装|安装|送货|地址": "涉及库存、物流或现场安排",
    r"排期|预约|师傅|人员|(?:安排|调配|通知).{0,8}(?:施工|安装|送货|工人|队伍)": "涉及人员或排期",
}

# These remove only a named everyday use of an ambiguous word. An additional
# business keyword in the same message still contributes its own risk.
CASUAL_MENTIONS = re.compile(
    r"(?:烘焙|做饭|做菜|蛋糕|饼干|面包|甜点)(?:用的?|的|所需的)?材料"
    r"|(?:真|很|挺|特别|太|非常|比较|实在)便宜(?!点|一些|一点|些|吗|不|多少|了)"
    r"|绝对(?:好笑|好吃|好看|精彩|有趣|无聊|难看)"
    r"|保温杯"
    r"|(?:手机|电脑|笔记本|平板|游戏|软件|系统)(?:的|新)?配置"
    r"|(?:手机|电脑|游戏|软件|系统)(?:的)?(?:桌面)?布局"
    r"|(?:手机|电脑|耳机|鼠标|键盘|闹钟|电视)(?:又|已经|突然|都)?坏了"
    r"|(?:电影|视频|小说|游戏|电视剧|动漫)(?:的)?质量问题",
    flags=re.IGNORECASE,
)
BUSINESS_OR_ACTION_REQUEST = re.compile(
    r"客户|订单|下单|成交|供货|采购|工地|工程|施工|装修|售后"
    r"|(?:你们?|您).{0,8}(?:做的|装的|卖的|送的|发的|提供|负责)"
    r"|(?:你们?|您).{0,12}(?:质量问题|坏了)"
    r"|(?:帮我|给我|替我|麻烦|请).{0,8}(?:准备|提供|采购|安装|重装|维修|修|配置|安排)"
    r"|坏了.{0,8}(?:退|换|修|赔|怎么办|怎么处理)"
    r"|质量问题.{0,12}(?:怎么处理|没解决)"
)

NUMBER = r"\d+(?:\.\d+)?\s*"
NUMERIC_DETAIL = re.compile(NUMBER + r"(?:元|万|块|号|日|米|平米)")
QUANTITY_REQUEST = re.compile(
    r"(?:要|需要|订|采购|供应|做|发|给|送|留)(?:我|你|他|她|我们|你们|他们)?\s*"
    + NUMBER + r"(?:天|周|个月|套|个)"
    r"|" + NUMBER + r"(?:天|周|个月|套|个).{0,8}(?:能(?:不能)?(?:做|给|发|送|交|提供)|可以(?:做|给|发|送|交|提供)|给我|帮我|要不要|行吗|可以吗|够吗)"
    r"|" + NUMBER + r"(?:套|个)\s*(?:箱子?|柜子?|门窗?|板材|设备|零件|组件|货|包裹)"
)
DATE = re.compile(
    NUMBER + r"(?:天|号|日|周|个月)|今天|明天|后天|本周|下周|周末|月底|年前"
)
DATE_ACTION = re.compile(
    r"能来|过来|出发|见面|见个面|到达|送到|送货|完成|做好|做完|交付|交货|开工|开始做|截止|到期|订票|订房|几点|什么时候"
    r"|约.{0,8}(?:吗|好不好|怎么样|几点)|(?:你|我们|咱们|一起).{0,6}约"
    r"|(?:给我|帮我|替我|麻烦|请).{0,8}安排"
)


def _matched(patterns: dict[str, str], text: str) -> list[str]:
    reasons: list[str] = []
    for pattern, reason in patterns.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            reasons.append(reason)
    return reasons


def assess_risk(text: str) -> RiskAssessment:
    normalized = " ".join(text.strip().split())
    topic_text = normalized
    if not BUSINESS_OR_ACTION_REQUEST.search(normalized):
        topic_text = CASUAL_MENTIONS.sub(" ", normalized)
    critical = _matched(CRITICAL_PATTERNS, normalized)
    high = _matched(HIGH_PATTERNS, topic_text)
    medium = _matched(MEDIUM_PATTERNS, topic_text)

    if critical:
        return RiskAssessment(
            level=RiskLevel.critical,
            reasons=critical + high + medium,
            safe_holding_reply="收到，这件事我先核对一下，涉及具体金额或承诺的内容我确认后再给你明确回复。",
        )
    if high:
        return RiskAssessment(
            level=RiskLevel.high,
            reasons=high + medium,
            safe_holding_reply="收到，这个我先把具体情况核对清楚，确认后给你一个准话。",
        )
    numeric_fact = bool(NUMERIC_DETAIL.search(normalized) or QUANTITY_REQUEST.search(normalized))
    date_arrangement = bool(DATE.search(normalized) and DATE_ACTION.search(normalized))
    if medium or numeric_fact or date_arrangement:
        reasons = medium or ["包含日期、数量、金额或交付信息"]
        return RiskAssessment(
            level=RiskLevel.medium,
            reasons=reasons,
            safe_holding_reply="这个我先核一下具体情况，确认完给你回。",
        )
    return RiskAssessment(level=RiskLevel.low, reasons=[])
