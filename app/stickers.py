"""Download only a verified sticker asset from WeChat's known CDN, locally."""
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
import hashlib,io,re,json,os,uuid,logging
import httpx
from PIL import Image

MAX_BYTES=2*1024*1024
MAX_PIXELS=4096*4096
MAX_FRAMES=240
CACHE_VERSION=2


class _HideMediaUrls(logging.Filter):
    def filter(self,record):
        message=record.getMessage()
        return not ('HTTP Request:' in message and re.search(r'https?://[^ /]+\.(?:qq\.com|qpic\.cn)/',message))


logging.getLogger('httpx').addFilter(_HideMediaUrls())


def _atomic(path,data):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def _frames(data,digest,directory):
    """Choose time-distributed distinct frames; composite transparency on white."""
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>MAX_PIXELS:raise ValueError('表情尺寸超出限制')
        extension={'GIF':'.gif','PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}.get(image.format)
        if extension is None:raise ValueError('不支持的表情图片格式')
        count=getattr(image,'n_frames',1)
        if count>MAX_FRAMES:raise ValueError('表情动画帧数超出限制')
        if image.width*image.height*count>64*1024*1024:raise ValueError('表情动画解码量超出限制')
        durations=[]
        for index in range(count):
            image.seek(index)
            durations.append(max(20,min(10000,int(image.info.get('duration',100) or 100))))
        total=sum(durations);selected=[]
        for fraction in (.15,.5,.85):
            elapsed=0
            for index,duration in enumerate(durations):
                elapsed+=duration
                if elapsed>=total*fraction:
                    selected.append(index);break
        paths=[];seen=set()
        for index in sorted(set(selected)):
            image.seek(index)
            foreground=image.convert('RGBA')
            background=Image.new('RGBA',foreground.size,'white')
            background.alpha_composite(foreground)
            frame=background.convert('RGB');frame.thumbnail((640,640))
            signature=hashlib.sha256(frame.tobytes()).hexdigest()
            if signature in seen:continue
            seen.add(signature)
            output=io.BytesIO();frame.save(output,format='PNG')
            path=directory/(digest+f'-{index}.png');_atomic(path,output.getvalue());paths.append(path)
        original=directory/(digest+extension);_atomic(original,data)
        manifest={'version':CACHE_VERSION,'md5_verified':True,'frames':[p.name for p in paths],
                  'frame_sha256':[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths],
                  'animated_frames':count,'duration_ms':total,'original':original.name}
        _atomic(directory/(digest+'.json'),json.dumps(manifest).encode('utf-8'))
        return paths


def _cached(digest,directory):
    try:
        cached=json.loads((directory/(digest+'.json')).read_text(encoding='utf-8'))
        original=directory/cached['original']
        if original.name!=cached['original'] or not re.fullmatch(digest+r'\.(gif|png|jpg|webp)',original.name):return None
        data=original.read_bytes()
        if len(data)>MAX_BYTES or hashlib.md5(data).hexdigest()!=digest:return None
        if cached.get('version')!=CACHE_VERSION:return _frames(data,digest,directory)
        names=cached['frames'];hashes=cached['frame_sha256']
        if not 1<=len(names)<=3 or len(names)!=len(hashes):return None
        paths=[]
        for name,expected in zip(names,hashes):
            if not re.fullmatch(digest+r'-\d+\.png',name):return None
            path=directory/name
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:return _frames(data,digest,directory)
            paths.append(path)
        return paths
    except (OSError,ValueError,KeyError,TypeError):
        return None


def metadata(text, *, require_url=True):
    match=re.search(r'<msg[\s>].*',text or '',re.S)
    if not match or len(match[0])>100000:raise ValueError('表情数据无效')
    if '<!DOCTYPE' in match[0].upper() or '<!ENTITY' in match[0].upper():raise ValueError('表情数据包含不支持的实体')
    node=ET.fromstring(match[0]).find('emoji')
    if node is None:raise ValueError('缺少表情节点')
    md5=node.get('md5','').lower()
    if not re.fullmatch(r'[0-9a-f]{32}',md5):raise ValueError('表情标识无效')
    url=node.get('cdnurl','');parsed=urlparse(url);host=parsed.hostname or ''
    if not url and not require_url:
        return {'md5':md5,'url':'','label':(node.get('attachedtext') or node.get('desc') or '')[:200]}
    if parsed.scheme not in {'http','https'} or not(host.endswith('.qq.com') or host.endswith('.qpic.cn')) or parsed.username or parsed.port not in {None,80,443}:
        raise ValueError('不是允许的微信媒体地址')
    return {'md5':md5,'url':url,'label':(node.get('attachedtext') or node.get('desc') or '')[:200]}


def acquire(raw,root,account_dirs=()):
    meta=metadata(raw,require_url=False);directory=Path(root)/'sticker_assets';directory.mkdir(parents=True,exist_ok=True)
    paths=_cached(meta['md5'],directory)
    if paths:return meta['md5'],[str(p) for p in paths],meta['label']
    # Account roots come only from the authenticated database reader, not XML.
    for account in account_dirs:
        account=Path(account).resolve();digest=meta['md5']
        candidates=list((account/'cache').glob('*/Emoticon/'+digest[:2]+'/'+digest))
        candidates.append(account/'business'/'emoticon'/'Persist'/digest[:2]/digest)
        for candidate in candidates:
            try:
                if not candidate.resolve().is_relative_to(account) or candidate.stat().st_size>MAX_BYTES:continue
                data=candidate.read_bytes()
                if hashlib.md5(data).hexdigest()!=digest:continue
                paths=_frames(data,digest,directory)
                return digest,[str(p) for p in paths],meta['label']
            except (OSError,ValueError):continue
    meta=metadata(raw)  # A missing URL is only usable with a verified local asset.
    data=bytearray()
    with httpx.stream('GET',meta['url'],timeout=8,follow_redirects=False,trust_env=False) as response:
        response.raise_for_status()
        for part in response.iter_bytes():
            data.extend(part)
            if len(data)>MAX_BYTES:raise ValueError('表情文件超出限制')
    if hashlib.md5(data).hexdigest()!=meta['md5']:raise ValueError('表情文件与消息校验值不一致')
    paths=_frames(bytes(data),meta['md5'],directory)
    return meta['md5'],[str(p) for p in paths],meta['label']
