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
    r"库存|现货|运输|物流|吊装|安装|地址": "涉及库存、物流或现场安排",
    r"安排|排期|预约|师傅|人员": "涉及人员或排期",
}

DATE_OR_MONEY = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:元|万|块|天|号|日|周|个月|套|个|米|平米))|(?:今天|明天|后天|本周|下周|月底|年前)"
)


def _matched(patterns: dict[str, str], text: str) -> list[str]:
    reasons: list[str] = []
    for pattern, reason in patterns.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            reasons.append(reason)
    return reasons


def assess_risk(text: str) -> RiskAssessment:
    normalized = " ".join(text.strip().split())
    critical = _matched(CRITICAL_PATTERNS, normalized)
    high = _matched(HIGH_PATTERNS, normalized)
    medium = _matched(MEDIUM_PATTERNS, normalized)

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
    date_match=DATE_OR_MONEY.search(normalized)
    numeric_fact=bool(re.search(r'\d+(?:\.\d+)?\s*(?:元|万|块|天|号|日|周|个月|套|个|米|平米)',normalized))
    date_arrangement=bool(date_match and re.search(r'能来|过来|出发|见面|见个面|到达|送到|送货|完成|做好|交付|开工|开始做|截止|到期|订票|订房|几点|什么时候|约|安排|计划',normalized))
    if medium or numeric_fact or date_arrangement:
        reasons = medium or ["包含日期、数量、金额或交付信息"]
        return RiskAssessment(
            level=RiskLevel.medium,
            reasons=reasons,
            safe_holding_reply="这个我先核一下具体情况，确认完给你回。",
        )
    return RiskAssessment(level=RiskLevel.low, reasons=[])
