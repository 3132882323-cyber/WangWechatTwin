import pytest
from app.stickers import metadata
from app.stickers import _frames, _cached
from app.stickers import acquire
from PIL import Image
import io,hashlib,json
import httpx
from app import stickers


def test_media_metadata_never_returns_keys_or_sender_identity():
    raw='<msg><emoji md5="'+'a'*32+'" cdnurl="https://emoji.qpic.cn/a" aeskey="secret" fromusername="private" attachedtext="谢谢" /></msg>'
    data=metadata(raw)
    assert data['label']=='谢谢'
    assert 'secret' not in str(data) and 'private' not in str(data)


def test_untrusted_media_address_is_rejected():
    for address in ['http://127.0.0.1/a','https://qq.com.evil.test/a','file:///secret','https://user:password@emoji.qpic.cn/a']:
        with pytest.raises(ValueError):metadata('<msg><emoji md5="'+'a'*32+'" cdnurl="'+address+'" /></msg>')


def test_transparency_is_readable_and_original_verified(tmp_path):
    image=Image.new('RGBA',(12,12),(0,0,0,0))
    image.putpixel((2,2),(0,0,0,255))
    output=io.BytesIO();image.save(output,format='PNG');data=output.getvalue()
    digest=hashlib.md5(data).hexdigest()
    paths=_frames(data,digest,tmp_path)
    with Image.open(paths[0]) as frame:
        assert frame.getpixel((0,0))==(255,255,255)
        assert frame.getpixel((2,2))==(0,0,0)
    assert _cached(digest,tmp_path)==paths
    paths[0].write_bytes(b'broken preview')
    assert _cached(digest,tmp_path)==paths
    (tmp_path/(digest+'.png')).write_bytes(b'broken original')
    assert _cached(digest,tmp_path) is None


def test_animation_sampling_uses_time_and_removes_duplicates(tmp_path):
    frames=[Image.new('RGB',(8,8),color) for color in ['red','blue','green']]
    output=io.BytesIO()
    frames[0].save(output,format='GIF',save_all=True,append_images=frames[1:],duration=[100,2000,100])
    data=output.getvalue();digest=hashlib.md5(data).hexdigest()
    paths=_frames(data,digest,tmp_path)
    assert len(paths)==1 and paths[0].name.endswith('-1.png')
    manifest=json.loads((tmp_path/(digest+'.json')).read_text())
    assert manifest['animated_frames']==3 and manifest['duration_ms']==2200


def test_cache_rejects_path_traversal(tmp_path):
    digest='a'*32
    (tmp_path/(digest+'.json')).write_text(json.dumps({'original':'../private.png'}))
    assert _cached(digest,tmp_path) is None


def test_md5_only_message_uses_verified_original_without_network(tmp_path):
    image=Image.new('RGB',(10,10),'blue');output=io.BytesIO();image.save(output,format='PNG')
    data=output.getvalue();digest=hashlib.md5(data).hexdigest()
    directory=tmp_path/'sticker_assets';directory.mkdir()
    _frames(data,digest,directory)
    result=acquire(f'<msg><emoji md5="{digest}"/></msg>',tmp_path)
    assert result[0]==digest and len(result[1])==1


def test_local_plaintext_asset_requires_matching_md5(tmp_path):
    image=Image.new('RGB',(10,10),'blue');output=io.BytesIO();image.save(output,format='PNG')
    data=output.getvalue();digest=hashlib.md5(data).hexdigest()
    account=tmp_path/'account';asset=account/'business'/'emoticon'/'Persist'/digest[:2]/digest
    asset.parent.mkdir(parents=True);asset.write_bytes(data)
    result=acquire(f'<msg><emoji md5="{digest}"/></msg>',tmp_path/'output',[account])
    assert result[0]==digest
    asset.write_bytes(b'wrong asset')
    with pytest.raises(ValueError):acquire(f'<msg><emoji md5="{digest}"/></msg>',tmp_path/'other-output',[account])


def test_vetted_cdn_retries_a_transient_failure_then_verifies_image(tmp_path,monkeypatch):
    image=Image.new('RGB',(10,10),'green');output=io.BytesIO();image.save(output,format='PNG')
    data=output.getvalue();digest=hashlib.md5(data).hexdigest()
    calls=[]

    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def raise_for_status(self):pass
        def iter_bytes(self):yield data

    def stream(*args,**kwargs):
        calls.append((args,kwargs))
        if len(calls)==1:raise httpx.ReadTimeout('temporary timeout')
        return Response()

    monkeypatch.setattr(stickers.httpx,'stream',stream)
    monkeypatch.setattr(stickers.time,'sleep',lambda _seconds:None)
    result=acquire(f'<msg><emoji md5="{digest}" cdnurl="https://emoji.qpic.cn/a"/></msg>',tmp_path)
    assert len(calls)==2
    assert result[0]==digest and len(result[1])==1
    assert calls[0][1]['follow_redirects'] is False and calls[0][1]['trust_env'] is False


def test_vetted_cdn_does_not_retry_forbidden_response(tmp_path,monkeypatch):
    calls=[];request=httpx.Request('GET','https://emoji.qpic.cn/a')

    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def raise_for_status(self):raise httpx.HTTPStatusError('forbidden',request=request,response=httpx.Response(403,request=request))

    def stream(*args,**kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(stickers.httpx,'stream',stream)
    with pytest.raises(httpx.HTTPStatusError):
        acquire('<msg><emoji md5="'+'a'*32+'" cdnurl="https://emoji.qpic.cn/a"/></msg>',tmp_path)
    assert len(calls)==1
