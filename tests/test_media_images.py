import io,struct
from PIL import Image
import pytest
from Crypto.Cipher import AES
from app.media_images import decrypt_v2_image,prepare_image,image_refs
from app.media_images import LocalImageReader
from app.models import IncomingMessage
from app.config import AppConfig
from types import SimpleNamespace
import hashlib,json


def test_v2_decryption_validates_padding_and_reconstructs_segments(tmp_path):
    key=b'1234567890abcdef';xor=91;plain=b'\xff\xd8\xff'+b'pixel-data'*3
    head=plain[:17];middle=plain[17:-4];tail=plain[-4:];padding=16-len(head)%16
    encrypted=AES.new(key,AES.MODE_ECB).encrypt(head+bytes([padding])*padding)
    data=b'\x07\x08V2\x08\x07'+struct.pack('<II',len(head),len(tail))+b'\x00'+encrypted+middle+bytes(b^xor for b in tail)
    path=tmp_path/'image.dat';path.write_bytes(data)
    assert decrypt_v2_image(path,key,xor)==plain
    with pytest.raises(ValueError):decrypt_v2_image(path,b'bad-key',xor)
    path.write_bytes(data[:18])
    with pytest.raises(ValueError):decrypt_v2_image(path,key,xor)


def test_long_screenshot_crops_preserve_text_scale_and_cover_all(tmp_path):
    image=Image.new('RGB',(1200,2600),'white');image.putpixel((10,10),(255,0,0));image.putpixel((10,2590),(0,0,255))
    data=io.BytesIO();image.save(data,format='PNG');paths,meta=prepare_image(data.getvalue(),tmp_path)
    assert len(paths)==3 and meta['image_partial'] is False
    with Image.open(paths[0]) as first,Image.open(paths[-1]) as last:
        assert first.width==1200 and first.getpixel((10,10))==(255,0,0)
        assert last.getpixel((10,last.height-10))==(0,0,255)


def test_very_long_image_is_not_claimed_complete(tmp_path):
    image=Image.new('RGB',(400,8000),'white');data=io.BytesIO();image.save(data,format='PNG')
    _,meta=prepare_image(data.getvalue(),tmp_path)
    assert meta['image_partial'] is True
    assert image_refs('md5="'+'a'*32+'"',b'\x01' + b'a'*32)==['a'*32]


def test_image_lookup_cannot_borrow_another_contacts_folder(tmp_path):
    cfg=AppConfig(project_root=tmp_path);runtime=tmp_path/'runtime';runtime.mkdir()
    account=tmp_path/'account';source=account/'db_storage'/'message'/'message_0.db'
    reader=SimpleNamespace(root=runtime,files={'message':{'source':str(source)}})
    digest='a'*32;folder=account/'msg/attach'/hashlib.md5(b'alice').hexdigest()/'2026-10/Img';folder.mkdir(parents=True)
    image=Image.new('RGB',(100,100),'blue');data=io.BytesIO();image.save(data,format='PNG');(folder/(digest+'.dat')).write_bytes(data.getvalue())
    resolver=LocalImageReader(cfg,reader)
    message=IncomingMessage(contact='bob',sender='bob',external_id='image',content='[图片]',message_type='image',raw_summary=json.dumps({'image_refs':[digest]}))
    assert resolver.resolve(message)[0]==[]
    assert len(resolver.resolve(message.model_copy(update={'contact':'alice'}))[0])==1
