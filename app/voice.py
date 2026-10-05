"""Match the database tail to visible messages before using WeChat transcription."""
from __future__ import annotations


def align_voice(expected, visible, target_index):
    # Text anchors plus order identify a particular voice, rather than 'last voice'.
    if len(expected) < 3 or sum(kind == 'text' and bool(text.strip()) for kind, text in expected) < 2:
        raise ValueError('语音缺少可核验的文字上下文')
    candidates = [i for i in range(len(visible) - len(expected) + 1)
                  if visible[i:i + len(expected)] == expected]
    if len(candidates) != 1 or candidates[0] + len(expected) != len(visible):
        raise ValueError('语音无法与当前聊天尾部唯一对应')
    if expected[target_index][0] != 'voice':
        raise ValueError('目标不是语音')
    # Adjacent indistinguishable voices lack individual identity.
    if ((target_index and expected[target_index-1][0] == 'voice') or
            (target_index + 1 < len(expected) and expected[target_index+1][0] == 'voice')):
        raise ValueError('连续语音无法逐条核验')
    return candidates[0] + target_index


def native_transcription(message):
    """Use the actual WeChat menu; do not patch the SDK or its licensing."""
    import time
    from wxauto4.ui.component import Menu

    def texts(control, depth=0):
        found = set()
        if depth > 8:
            return found
        if getattr(control, 'ControlTypeName', '') == 'TextControl':
            name = str(control.Name).strip()
            if name:
                found.add(name)
        for child in control.GetChildren():
            found.update(texts(child, depth+1))
        return found

    before = texts(message.control)
    message.control.RightClick()
    menu = Menu(message.parent)
    if '转文字' not in menu.option_names:
        message.control.SendKeys('{Esc}')
        raise ValueError('微信未提供这条语音的转文字菜单')
    menu.select('转文字')
    ignored = {'转文字','收起','取消','转换中','转换中...','转换失败','重新转换','无法识别'}
    for _ in range(20):
        added = texts(message.control) - before - ignored
        added = {s for s in added if len(s) > 2 and '秒' not in s and not s.startswith('语音')}
        if len(added) == 1:
            return added.pop()
        time.sleep(.5)
    raise ValueError('微信转写正文未能唯一识别')
