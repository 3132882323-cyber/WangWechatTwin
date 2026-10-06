import pytest
from app.stickers import metadata


def test_media_metadata_never_returns_keys_or_sender_identity():
    raw='<msg><emoji md5="'+'a'*32+'" cdnurl="https://emoji.qpic.cn/a" aeskey="secret" fromusername="private" attachedtext="谢谢" /></msg>'
    data=metadata(raw)
    assert data['label']=='谢谢'
    assert 'secret' not in str(data) and 'private' not in str(data)


def test_untrusted_media_address_is_rejected():
    for address in ['http://127.0.0.1/a','https://qq.com.evil.test/a','file:///secret','https://user:password@emoji.qpic.cn/a']:
        with pytest.raises(ValueError):metadata('<msg><emoji md5="'+'a'*32+'" cdnurl="'+address+'" /></msg>')
