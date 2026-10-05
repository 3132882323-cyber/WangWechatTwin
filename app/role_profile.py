"""Describe observed communication habits without inventing personality or facts."""
from __future__ import annotations

import json
import re
from collections import Counter
from statistics import median


def situation(text):
    if re.fullmatch(r'(?:你好|您好|在吗|在不|早上好|晚上好|哈喽|hello|hi)[。.!！?？\s]*', text.strip(), re.I):
        return 'greeting'
    if re.fullmatch(r'(?:好的?|收到|行|可以|对|嗯+|ok|谢谢)[。.!！\s]*', text.strip(), re.I):
        return 'acknowledgment'
    for category, pattern in [
        ('personal_chat', r'相亲|恋爱|女朋友|男朋友|谈对象|结婚|失恋'),
        ('reason', r'为什么|为啥|什么原因|怎么想|咋想'),
        ('progress', r'进度|怎么样了|弄好|做好|到哪|完成了|处理好'),
        ('information', r'发我|发一下|发给我|给我发|照片|图片|资料|地址|位置|什么|哪里|哪儿'),
        ('arrangement', r'明天|今天|几点|什么时候|过来|见面|吃饭|有空|时间'),
        ('request', r'能不能|可以吗|行不行|帮我|麻烦|要不要|方便|能否'),
    ]:
        if re.search(pattern, text):
            return category
    return 'conversation'


def response_forms(text):
    forms = []
    patterns = {
        '直接确认': r'^(?:ok|好|行|对|是的|收到|可以)[。.!！\s]*$',
        '拒绝或指出限制': r'没有|不行|不可以|不能|没办法|自己搜|不用|不需要',
        '追问具体信息': r'什么|哪个|多少|哪里|怎么|发我|发一下|\?|？',
        '暂缓并核实': r'我看看|我看一下|核一下|确认|等一下|稍等|再说|回头',
        '给出下一步': r'你先|先把|你把|直接|去找|联系|发给|找一下',
        '直接说明不知道或不愿意': r'不懂|不会|不知道|忘了|记不清|没兴趣|不喜欢|不想|懒得',
        '闲聊中的打趣或自嘲': r'哈哈|笑死|懒啊|我笨|我傻',
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text, re.I):
            forms.append(label)
    return forms


def summarize(texts):
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return {'sample_count': 0, 'evidence_level': 'insufficient'}
    n = len(texts)
    forms = Counter(label for t in texts for label in response_forms(t))
    acknowledgments = Counter(t for t in texts if re.fullmatch(r'(?:OK|ok|好|好的|行|对|是的|收到|可以|嗯+|没事)[。.!！\s]*', t))
    openers = Counter(t for t in texts if re.fullmatch(r'(?:咋了|咋说|咋回事|啥事|啥|干嘛|怎么了|什么事|有什么事|你说|说吧|我在|在)[。.!！?？\s]*',t))
    known_faces = {'[捂脸]','[呲牙]','[微笑]','[偷笑]','[坏笑]','[笑哭]','[旺柴]','[裂开]','[机智]','[尴尬]','[害羞]','[调皮]','[奸笑]','[汗]','[强]','[抱拳]','[OK]','[握手]','[哭笑]'}
    faces = Counter(face for t in texts for face in re.findall(r'\[[^\]]+\]', t) if face in known_faces)
    face_only = sum(t in known_faces or bool(re.fullmatch(r'[\U0001F600-\U0001F64F]+',t)) for t in texts)
    return {
        'sample_count': n,
        'evidence_level': 'supported' if n >= 20 else 'limited',
        'median_characters': median(map(len, texts)),
        'short_under_20_ratio': round(sum(len(t) <= 20 for t in texts)/n, 3),
        'no_end_punctuation_ratio': round(sum(not re.search(r'[。.!！?？…]$', t) for t in texts)/n, 3),
        'question_ratio': round(sum(bool(re.search(r'[?？]|什么|哪个|怎么|多少',t)) for t in texts)/n, 3),
        'exclamation_ratio': round(sum(bool(re.search(r'[!！]',t)) for t in texts)/n, 3),
        'response_forms': dict(forms),
        'acknowledgments': acknowledgments.most_common(6),
        'common_conversation_openers': openers.most_common(6),
        'standalone_face_ratio': round(face_only/n,3),
        'familiar_face_spellings': faces.most_common(6),
        'interpretation_boundary': '统计描述表达习惯，不证明脾气、职业、亲疏关系或当前决定；不能因过去答应/拒绝就自动沿用立场',
    }


def load(path, contact, incoming):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'evidence_level': 'insufficient'}
    category = situation(incoming)
    own = data.get('contacts', {}).get(contact, {})
    preferred = own.get('style', {}) if own.get('style', {}).get('sample_count', 0) >= 20 else data.get('global', {})
    guidance = []
    if preferred.get('short_under_20_ratio',0) >= .75:
        guidance.append('平常表达偏短；先用最必要的几个字或一句话回答，复杂事情不为模仿短句而省略关键事实')
    if preferred.get('no_end_punctuation_ratio',0) >= .8:
        guidance.append('短消息通常不补句号，不把 OK 强制改成“好的，收到。”；大小写参考这位联系人的样例')
    forms = preferred.get('response_forms', {})
    if forms.get('拒绝或指出限制',0) >= 3:
        guidance.append('确实不能或不愿意时可以直接说，不为了显得热情而答应；拒绝依据必须是当前事实或本人明确立场')
    if forms.get('直接说明不知道或不愿意',0) >= 3:
        guidance.append('记录里会直接表达不知道、不会、不想等情况；没有依据时不替本人补体面的理由或心理动机')
    if forms.get('闲聊中的打趣或自嘲',0) >= 3:
        guidance.append('闲聊可参考本人已有的打趣/自嘲方式；不强行搞笑，也不据此判定本人总是某种性格')
    if preferred.get('standalone_face_ratio',0) >= .08:
        guidance.append('这段交流会用短表情回应轻松闲聊，可参考本人已有的表情写法，不必把每个反应解释成长句；重要事项仍要说清')
    return {
        'global_observed_habits': data.get('global', {}),
        'current_contact_habits': own.get('style', {}),
        'current_situation': category,
        'same_contact_situation_habits': own.get('situations', {}).get(category, {}),
        'observed_expression_guidance': guidance,
        'opener_rule': '询问在吗时，不一定要回答“在的”；参考这位联系人的本人问话方式，例如本人常用的咋了/啥事/你说。不要编造当前正忙或正在做什么。',
        'reply_process': [
            '先辨认这位联系人此刻想要信息、安排、确认还是普通闲聊；不凭语气猜关系',
            '根据当前事实与本人已确认立场决定答应、拒绝、追问或暂缓；历史仅是参考',
            '优先使用本人对这位联系人、这种情境的表达方式和长短；本人修改优先于旧样例',
            '只把最终回复写出来；不附分析、不刻意报本人姓名、不添加模板客套',
        ],
    }
