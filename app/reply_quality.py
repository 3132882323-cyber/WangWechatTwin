"""Owner correction: understand the message, do not default to empty laughter."""
import re


def laughter_only(text):
    plain=re.sub(r'[\s。.!！?？,，…~～]+','',text or '')
    return bool(plain and re.fullmatch(r'(?:哈{2,}|呵{2,}|嘿{2,}|(?:ha){2,}|lol|lmao|笑死(?:我)?了?|笑不活了|乐死(?:我)?了?|😂|🤣|😆|😄|\[笑哭\]|\[呲牙\])+',plain,re.I))


def trim_laughter(text):
    text=re.sub(r'^(?:哈{2,}[，,！!。.\s]*)+','',text or '')
    return re.sub(r'[，,\s]*(?:哈{2,}[。.!！\s]*)+$','',text).strip()


def substantive_examples(value):
    """Filter prompt examples only; original history and statistics remain intact."""
    if isinstance(value,dict):return {key:substantive_examples(item) for key,item in value.items()}
    if isinstance(value,list):
        output=[]
        for item in value:
            if isinstance(item,str) and laughter_only(item):continue
            if isinstance(item,(list,tuple)) and item and isinstance(item[0],str) and laughter_only(item[0]):continue
            if isinstance(item,dict) and any(laughter_only(item.get(key,'')) for key in ['reply','preferred_reply']):continue
            output.append(substantive_examples(item))
        return output
    return value
