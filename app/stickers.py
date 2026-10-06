"""Download only a verified sticker asset from WeChat's known CDN, locally."""
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
import hashlib,io,re,json
import httpx
from PIL import Image


def metadata(text):
    match=re.search(r'<msg[\s>].*',text or '',re.S)
    if not match or len(match[0])>100000:raise ValueError('表情数据无效')
    node=ET.fromstring(match[0]).find('emoji')
    if node is None:raise ValueError('缺少表情节点')
    md5=node.get('md5','').lower()
    if not re.fullmatch(r'[0-9a-f]{32}',md5):raise ValueError('表情标识无效')
    url=node.get('cdnurl','');parsed=urlparse(url);host=parsed.hostname or ''
    if parsed.scheme not in {'http','https'} or not(host.endswith('.qq.com') or host.endswith('.qpic.cn')) or parsed.username or parsed.port not in {None,80,443}:
        raise ValueError('不是允许的微信媒体地址')
    return {'md5':md5,'url':url,'label':(node.get('attachedtext') or node.get('desc') or '')[:200]}


def acquire(raw,root):
    meta=metadata(raw);directory=Path(root)/'sticker_assets';directory.mkdir(parents=True,exist_ok=True)
    paths=[directory/(meta['md5']+'-0.png')]
    manifest=directory/(meta['md5']+'.json')
    if manifest.exists():
        cached=json.loads(manifest.read_text(encoding='utf-8'))
        paths=[directory/name for name in cached['frames']]
        if all(p.exists() for p in paths):return meta['md5'],[str(p) for p in paths],meta['label']
    data=bytearray()
    with httpx.stream('GET',meta['url'],timeout=8,follow_redirects=False,trust_env=False) as response:
        response.raise_for_status()
        for part in response.iter_bytes():
            data.extend(part)
            if len(data)>2*1024*1024:raise ValueError('表情文件超出限制')
    if hashlib.md5(data).hexdigest()!=meta['md5']:raise ValueError('表情文件与消息校验值不一致')
    image=Image.open(io.BytesIO(data))
    if image.width*image.height>4096*4096:raise ValueError('表情尺寸超出限制')
    count=getattr(image,'n_frames',1);indexes=sorted({0,count//2,count-1});paths=[]
    for index in indexes:
        image.seek(index);frame=image.convert('RGB');frame.thumbnail((640,640))
        path=directory/(meta['md5']+f'-{index}.png');frame.save(path);paths.append(path)
    manifest.write_text(json.dumps({'md5_verified':True,'frames':[p.name for p in paths],'animated_frames':count}),encoding='utf-8')
    return meta['md5'],[str(p) for p in paths],meta['label']
