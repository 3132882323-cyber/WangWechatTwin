"""Conservative local ASR quality checks; hints never replace heard words."""
import re

def assess_transcript(text,segments):
    reasons=[];words=[]
    for segment in segments:
        if segment.avg_logprob < -1:reasons.append('低转写置信度')
        if segment.no_speech_prob > .6:reasons.append('可能静音或背景声')
        if getattr(segment,'compression_ratio',0)>2.4:reasons.append('疑似重复幻听')
        words.extend(word.probability for word in (getattr(segment,'words',None) or []))
    if words and (min(words)<.35 or sum(words)/len(words)<.65):reasons.append('存在听不清的词')
    if re.search(r'\d|[一二两三四五六七八九十百千万]+(?:元|号|点|天|月|年|块|分钟|小时)',text):reasons.append('数字金额或日期需核对')
    if re.search(r'(?:今天|明天|后天|下周).{0,8}(?:送|到|来|完成|付款)|(?:验证码|密码|银行卡|付款|转账|合同|保证)',text):reasons.append('敏感信息或安排需核对')
    return list(dict.fromkeys(reasons))
