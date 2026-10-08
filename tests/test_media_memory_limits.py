import hashlib
import hmac
import io
import json
import shutil
import sqlite3
import struct
import sys
import tracemalloc
import wave
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard

from app.adapters.history_reader import authenticated_snapshot, bounded_decompress, wal_checksum
from app.config import AppConfig
from app.media_voice import OfflineVoiceProcessor, _BoundedPCMWriter, resolve_voice
from app.models import IncomingMessage


def wal_fixture(tmp_path, monkeypatch, frames, tail=b''):
    key, salt = b'\x01'*32, b'\x00'*16
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(value^0x3a for value in salt), 2, dklen=32)

    def page(number, marker):
        payload = marker*4032
        mac = hmac.new(mac_key, payload[16 if number == 1 else 0:]+struct.pack('<I', number), hashlib.sha512).digest()
        return payload+mac

    source = tmp_path/'source.db'
    source.write_bytes(page(1,b'A')+page(2,b'B'))
    wal = Path(str(source)+'-wal')
    header = struct.pack('>6I', 0x377f0682, 3007000, 4096, 0, 7, 9)
    state = wal_checksum(header)
    with wal.open('wb') as output:
        output.write(header+struct.pack('>2I', *state))
        for number, size, marker in frames:
            data = page(number, marker)
            frame_header = struct.pack('>4I', number, size, 7, 9)
            state = wal_checksum(frame_header[:8]+data, state)
            output.write(frame_header+struct.pack('>2I', *state)+data)
        output.write(tail)

    # Real page HMAC/checksums remain enabled; plaintext is unnecessary for this replay test.
    monkeypatch.setattr('app.adapters.history_crypto._decrypt_page', lambda key, data, number: data)
    class CheckedConnection:
        def execute(self, query):
            assert query == 'PRAGMA quick_check'
            return self
        def fetchall(self):
            return [('ok',)]
        def close(self):
            pass
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: CheckedConnection())
    directory = tmp_path/'snapshot'
    directory.mkdir()
    return {'source':str(source),'enc_key':key.hex(),'salt':salt.hex()}, directory, page, wal


def test_streaming_wal_replays_latest_commit_and_ignores_uncommitted_tail(tmp_path, monkeypatch):
    info,directory,page,_ = wal_fixture(tmp_path, monkeypatch,
        [(2,2,b'C'),(2,0,b'D'),(3,3,b'E'),(3,0,b'F')], tail=b'incomplete')
    target,_ = authenticated_snapshot(info, directory)
    assert target.read_bytes() == page(1,b'A')+page(2,b'D')+page(3,b'E')


def test_streaming_wal_honors_committed_database_shrink(tmp_path, monkeypatch):
    info,directory,page,_ = wal_fixture(tmp_path, monkeypatch, [(2,2,b'C'),(1,1,b'D'),(2,0,b'E')])
    target,_ = authenticated_snapshot(info, directory)
    assert target.read_bytes() == page(1,b'D')


def test_large_wal_has_constant_memory_and_never_uses_read_bytes(tmp_path, monkeypatch):
    count = 2048
    info,directory,page,wal = wal_fixture(tmp_path, monkeypatch, ((2,2,b'C') for _ in range(count)))
    original_read = Path.read_bytes
    def guarded_read(path):
        if path.name.endswith('-wal'):
            raise AssertionError('WAL must remain on disk')
        return original_read(path)
    monkeypatch.setattr(Path, 'read_bytes', guarded_read)
    tracemalloc.start()
    try:
        target,_ = authenticated_snapshot(info, directory)
        _,peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert target.read_bytes() == page(1,b'A')+page(2,b'C')
    assert peak < 5*1024*1024
    print(json.dumps({'wal_bytes':wal.stat().st_size,'python_peak_bytes':peak}))


def test_decompressed_message_limit_applies_to_declared_and_unknown_sizes(monkeypatch):
    declared = zstandard.ZstdCompressor().compress(b'x'*129)
    original = zstandard.ZstdDecompressor
    monkeypatch.setattr(zstandard, 'ZstdDecompressor', lambda: pytest.fail('oversized declared frame reached allocator'))
    with pytest.raises(ValueError, match='超出读取限制'):
        bounded_decompress(declared, max_bytes=128)
    monkeypatch.setattr(zstandard, 'ZstdDecompressor', original)
    unknown = zstandard.ZstdCompressor(write_content_size=False).compress(b'x'*129)
    with pytest.raises(zstandard.ZstdError):
        bounded_decompress(unknown, max_bytes=128)
    valid = zstandard.ZstdCompressor(write_content_size=False).compress(b'valid')
    assert bounded_decompress(valid, max_bytes=128) == b'valid'
    with pytest.raises(zstandard.ZstdError):
        bounded_decompress(valid[:-1], max_bytes=128)


def test_voice_decoder_rejects_excess_duration_before_model_load(tmp_path, monkeypatch):
    cfg = AppConfig(project_root=tmp_path)
    cfg.media.max_voice_seconds = 1
    root = tmp_path/'reader'
    root.mkdir()
    model = cfg.resolve(cfg.media.voice_model)
    model.mkdir(parents=True)
    (model/'model.bin').write_bytes(b'model-placeholder')
    silk = root/'voice.silk'
    silk.write_bytes(b'#!SILK_V3')
    monkeypatch.setattr('app.media_voice.resolve_voice', lambda reader,message:(silk,'synthetic'))
    chunks = []
    def decode(source, output, rate):
        assert isinstance(output, _BoundedPCMWriter)
        for _ in range(2):
            output.write(b'\x00'*48000)
            chunks.append(1)
    monkeypatch.setitem(sys.modules, 'pysilk', SimpleNamespace(decode=decode))
    processor = OfflineVoiceProcessor(cfg,SimpleNamespace(root=root))
    message = IncomingMessage(external_id='voice-limit',contact='peer',sender='peer',content='[语音]',message_type='voice')
    try:
        with pytest.raises(ValueError, match='时长超出'):
            processor._transcribe(message)
        assert chunks == [1]
        assert processor.model is None
    finally:
        processor.close()


def test_consumed_media_does_not_accumulate_hidden_executor_jobs(tmp_path, monkeypatch):
    cfg = AppConfig(project_root=tmp_path)
    cfg.media.voice_enabled = True
    root = tmp_path/'reader'
    root.mkdir()
    personal = SimpleNamespace(observed=lambda identity: identity == 'owner-voice')
    processor = OfflineVoiceProcessor(cfg,SimpleNamespace(root=root,personal=personal))
    seen = IncomingMessage(external_id='seen-voice',contact='peer',sender='peer',content='[语音]',message_type='voice')
    own = IncomingMessage(external_id='owner-voice',contact='peer',sender='本人',content='[语音]',message_type='voice',raw_summary='{"owner_message":true}')
    processor.db.add_incoming(seen,'low')
    queued = Future()
    processor.pending[own.external_id] = (own,queued)
    monkeypatch.setattr(processor.pool,'submit',lambda *args: pytest.fail('already consumed media resubmitted'))
    try:
        for _ in range(100):
            assert processor.poll([seen,own]) == []
        assert queued.cancelled()
        assert not processor.pending
    finally:
        processor.close()


@pytest.mark.parametrize('mode', ['duplicates','ambiguous','oversized'])
def test_voice_blob_reads_remain_bounded_and_uniquely_bound(tmp_path, monkeypatch, mode):
    root = tmp_path/'reader'
    root.mkdir()
    source = root/'media_0.db'
    payload = b'\x02#!SILK_V3'+b'a'*65536
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE Name2Id(user_name TEXT)')
        db.execute('INSERT INTO Name2Id VALUES(?)',('peer',))
        db.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,svr_id INTEGER,local_id INTEGER,voice_data BLOB)')
        if mode == 'duplicates':
            db.executemany('INSERT INTO VoiceInfo VALUES(1,99,1,?)', ((payload,) for _ in range(64)))
        elif mode == 'ambiguous':
            db.executemany('INSERT INTO VoiceInfo VALUES(1,99,1,?)', [(payload,),(payload+b'b',)])
        else:
            db.execute('INSERT INTO VoiceInfo VALUES(1,99,1,?)',(b'\x02#!SILK_V3'+b'a'*(5*1024*1024),))
    (root/'media_keys.json').write_text(json.dumps({'media_0.db':{'source':str(source)}}))
    def snapshot(info,directory):
        target = directory/'copy.db'
        shutil.copyfile(info['source'],target)
        return target,None
    monkeypatch.setattr('app.media_voice.authenticated_snapshot',snapshot)
    message = IncomingMessage(external_id='voice-row-limit',contact='peer',sender='peer',content='[语音]',message_type='voice',raw_summary='{"server_id":99}')
    reader = SimpleNamespace(root=root)
    tracemalloc.start()
    try:
        if mode == 'duplicates':
            asset,digest = resolve_voice(reader,message)
            assert asset.read_bytes() == payload
            assert digest == hashlib.sha256(payload).hexdigest()
        else:
            with pytest.raises(ValueError,match='唯一对应' if mode == 'ambiguous' else '大小不受支持'):
                resolve_voice(reader,message)
        _,peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2*1024*1024


def test_installed_silk_decoder_streams_into_bounded_wav(tmp_path):
    pysilk = pytest.importorskip('pysilk')
    encoded = io.BytesIO()
    pysilk.encode(io.BytesIO(b'\x00'*24000), encoded, 24000, 24000)
    encoded.seek(0)
    wav = tmp_path/'streamed.wav'
    with wave.open(str(wav),'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        bounded = _BoundedPCMWriter(output,48000)
        pysilk.decode(encoded,bounded,24000)
    with wave.open(str(wav),'rb') as reader:
        assert 0 < reader.getnframes() <= 24000
        assert reader.getnchannels() == 1
    assert bounded.size <= 48000
    encoded.seek(0)
    limited_wav = tmp_path/'limited.wav'
    with wave.open(str(limited_wav),'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        limited = _BoundedPCMWriter(output,960)
        with pytest.raises(ValueError,match='时长超出'):
            pysilk.decode(encoded,limited,24000)
    assert limited.size <= 960
