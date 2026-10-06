"""Private, explicitly approved image library. Never accepts a model file path."""
from pathlib import Path
import hashlib,json,re


def item(config,digest):
    if not config.stickers.enabled or not re.fullmatch(r'[0-9a-f]{32}',digest or ''):
        raise ValueError('表情包未启用或标识无效')
    catalog=json.loads(config.resolve(config.stickers.catalog).read_text(encoding='utf-8'))
    match=next((entry for entry in catalog.get('items',[]) if entry.get('id')==digest and entry.get('approved') is True),None)
    if not match:raise ValueError('表情包尚未批准加入发送库')
    root=config.resolve(config.paths.history_reader)/'sticker_assets'
    original=(root/match['original']).resolve();preview=(root/match['preview']).resolve()
    if original.parent!=root.resolve() or preview.parent!=root.resolve():raise ValueError('表情包路径越界')
    data=original.read_bytes()
    if len(data)>2*1024*1024 or hashlib.md5(data).hexdigest()!=digest:raise ValueError('表情包原图校验失败')
    if hashlib.sha256(preview.read_bytes()).hexdigest()!=match['preview_sha256']:raise ValueError('表情包预览校验失败')
    return {**match,'original_path':original,'preview_path':preview}


def suggest(config,content):
    if not config.stickers.enabled:return None
    if not re.search(r'考过了|上岸了|拿下了|成功了|拿到offer',content,re.I):return None
    try:
        catalog=json.loads(config.resolve(config.stickers.catalog).read_text(encoding='utf-8'))
        for entry in catalog.get('items',[]):
            if 'celebrate' in entry.get('tags',[]):return item(config,entry['id'])
    except (OSError,ValueError,KeyError,TypeError):return None
    return None
