"""Read authenticated local DB snapshots. This adapter cannot send messages."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import sqlite3
import struct
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import zstandard

from app.adapters.base import MessageAdapter
from app.models import IncomingMessage


class SnapshotBusyError(RuntimeError):
    """The source changed while copying; retry reading, never retry sending."""


def wal_checksum(data, state=(0, 0), endian="<"):
    values = struct.unpack(endian + str(len(data) // 4) + "I", data)
    a, b = state
    for i in range(0, len(values), 2):
        a = (a + values[i] + b) & 0xffffffff
        b = (b + values[i + 1] + a) & 0xffffffff
    return a, b


def signature(source):
    return tuple((f.stat().st_size, f.stat().st_mtime_ns) if f.exists() else None
                 for f in (source, Path(str(source) + "-wal")))


def authenticated_snapshot(info, directory):
    from app.adapters.history_crypto import _decrypt_page
    source = Path(info["source"])
    encrypted, target = directory / "encrypted.db", directory / "readable.db"
    before = signature(source)
    shutil.copyfile(source, encrypted)
    wal_source = Path(str(source) + "-wal")
    wal_snapshot = directory / "encrypted.db-wal"
    if wal_source.exists():
        shutil.copyfile(wal_source, wal_snapshot)
    if signature(source) != before:
        raise SnapshotBusyError("微信记录正在更新，稍后重试读取")
    key, salt = bytes.fromhex(info["enc_key"]), bytes.fromhex(info["salt"])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3a for v in salt), 2, dklen=32)

    def authenticate(page, pg):
        if len(page) != 4096:
            raise RuntimeError("微信数据库认证失败，停止本次读取")
        payload = page[16 if pg == 1 else 0:4032]
        expected = hmac.new(mac_key, payload + struct.pack("<I", pg), hashlib.sha512).digest()
        if not hmac.compare_digest(expected, page[4032:]):
            raise RuntimeError("微信数据库认证失败，停止本次读取")

    def decode(page, pg):
        authenticate(page, pg)
        return _decrypt_page(key, page, pg)

    with encrypted.open("rb") as src, target.open("wb") as dst:
        pg = 0
        while page := src.read(4096):
            pg += 1
            dst.write(decode(page, pg))
    if wal_snapshot.exists():
        with wal_snapshot.open("rb") as wal:
            wal_header = wal.read(32)
            if len(wal_header) == 32:
                magic, version, page_size, seq, s1, s2, c1, c2 = struct.unpack(">8I", wal_header)
                if magic not in (0x377f0682, 0x377f0683) or page_size != 4096:
                    raise RuntimeError("微信日志格式尚不支持")
                endian = "<" if magic == 0x377f0682 else ">"
                state = wal_checksum(wal_header[:24], endian=endian)
                if state != (c1, c2):
                    raise RuntimeError("微信日志头校验失败")
                commit_end, db_size = 0, 0
                # Keep only the last verified commit offset, never all WAL pages.
                while True:
                    header, page = wal.read(24), wal.read(4096)
                    if len(header) != 24 or len(page) != 4096:
                        break
                    pg, size, fs1, fs2, fc1, fc2 = struct.unpack(">6I", header)
                    if (fs1, fs2) != (s1, s2) or pg == 0:
                        break
                    expected = wal_checksum(header[:8] + page, state, endian)
                    if expected != (fc1, fc2):
                        break
                    state = expected
                    authenticate(page, pg)
                    if size:
                        commit_end, db_size = wal.tell(), size
                if commit_end:
                    wal.seek(32)
                    with target.open("r+b") as dst:
                        while wal.tell() < commit_end:
                            header, page = wal.read(24), wal.read(4096)
                            pg = struct.unpack(">I", header[:4])[0]
                            dst.seek((pg - 1) * 4096)
                            dst.write(decode(page, pg))
                        dst.truncate(db_size * 4096)
    connection = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("微信数据库副本完整性检查失败")
    finally:
        connection.close()
    return target, before


def bounded_decompress(data, max_bytes=8*1024*1024):
    """Enforce the limit even when a Zstd frame declares its output size."""
    declared = zstandard.frame_content_size(data)
    if declared not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and declared > max_bytes:
        raise ValueError("微信消息解压大小超出读取限制")
    decoded = zstandard.ZstdDecompressor().decompress(data, max_output_size=max_bytes)
    if len(decoded) > max_bytes:
        raise ValueError("微信消息解压大小超出读取限制")
    return decoded


class HistoryReadOnlyAdapter(MessageAdapter):
    def __init__(self, config):
        self.config = config
        self.root = config.resolve(config.paths.history_reader)
        self.keys = json.loads((self.root / "keys.json").read_text(encoding="utf-8"))
        self.files = {name: value for name, value in self.keys.items()
                      if re.search(r"/message/message_\d+\.db$", name.replace("\\", "/"))}
        if not self.files:
            raise RuntimeError("尚未连接可读取的微信消息库")
        self.cursors, self.signatures, self.names = {}, {}, {}
        self.contact_aliases, self.self_alias = {}, None
        self.self_username, self.self_display_name = None, None
        self.existing_conversations = set()
        self.contact_signatures = {}
        self.started_at = int(time.time())
        from app.personal_memory import PersonalMemory
        self.personal=PersonalMemory(config.resolve(config.paths.personal_database))
        self.personal.import_history(config.resolve(config.paths.chat_memory))
        self.own_media=[]
        self._load_names()
        self.ready = False
        self._read(initial=True)
        self.ready = True

    def _load_names(self):
        self.names, self.contact_aliases = {}, {}
        for name, info in self.keys.items():
            if not name.replace("\\", "/").endswith("/contact/contact.db"):
                continue
            with tempfile.TemporaryDirectory(prefix="contacts-", dir=self.root) as tmp:
                snapshot, observed = authenticated_snapshot(info, Path(tmp))
                conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    owner = name.split("/")[0].rsplit("_", 1)[0]
                    for username, remark, nickname, alias in conn.execute("SELECT username,remark,nick_name,alias FROM contact WHERE COALESCE(delete_flag,0)=0"):
                        self.names[username] = remark or nickname or username
                        self.contact_aliases[username] = alias
                        if username == owner:
                            self.self_alias = alias or username
                            self.self_username, self.self_display_name = username, nickname or remark
                    self.contact_signatures[name] = observed
                finally:
                    conn.close()
        (self.root / "contact_labels.json").write_text(json.dumps(self.names, ensure_ascii=False), encoding="utf-8")

    def doctor(self):
        return {"ok": self.ready, "adapter": "history_readonly", "message_databases": len(self.files),
                "historical_tables_baselined": len(self.cursors), "sending_supported": False}

    def _read(self, initial=False):
        result = []
        memory_rows = []
        for name, info in self.files.items():
            source = Path(info["source"])
            current = signature(source)
            if not initial and current == self.signatures.get(name):
                continue
            with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.root) as tmp:
                snapshot, observed = authenticated_snapshot(info, Path(tmp))
                conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    users = dict(conn.execute("SELECT rowid,user_name FROM Name2Id"))
                    account = name.split("/")[0].rsplit("_", 1)[0]
                    own_id = next((i for i, user in users.items() if user == account), None)
                    if own_id is None:
                        raise RuntimeError("无法核验本人的发送者标识，停止读取")
                    conversations = {hashlib.md5(user.encode()).hexdigest(): user for user in users.values()}
                    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                              if re.fullmatch(r"Msg_[0-9a-f]{32}", r[0])]
                    for table in tables:
                        contact = conversations.get(table[4:])
                        if contact:
                            self.existing_conversations.add(contact)
                        cursor_key = name + "/" + table
                        high = conn.execute(f'SELECT max(local_id) FROM "{table}"').fetchone()[0] or 0
                        previous = self.cursors.get(cursor_key)
                        if initial:
                            # Existing messages never enter the reply queue on startup.
                            self.cursors[cursor_key] = high
                            continue
                        new_table = previous is None
                        if new_table:
                            previous = 0
                        if high <= previous:
                            if high < previous:
                                self.cursors[cursor_key] = high
                            continue
                        if not contact or contact.endswith("@chatroom") and not self.config.wechat.allow_groups:
                            self.cursors[cursor_key] = high
                            continue
                        if self.config.wechat.sender_all_existing_chats and (contact.startswith("gh_") or contact in {
                                "filehelper", "weixin", "qqmail", "fmessage", "medianote", "newsapp", "notification_messages"}):
                            self.cursors[cursor_key] = high
                            continue
                        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                        source_cols = "source,WCDB_CT_source" if "source" in columns else "NULL,NULL"
                        packed_col='packed_info_data' if 'packed_info_data' in columns else 'NULL'
                        last_own_id = conn.execute(
                            f'SELECT max(local_id) FROM "{table}" WHERE local_id>? AND real_sender_id=?',
                            (previous, own_id)).fetchone()[0] or previous
                        rows = conn.execute(
                            f'SELECT local_id,server_id,local_type,real_sender_id,create_time,message_content,'
                            f'WCDB_CT_message_content,{source_cols},{packed_col} FROM "{table}" WHERE local_id>? ORDER BY local_id',
                            (previous,))
                        for local_id, server_id, typ, sender, ts, content, compression, source_text, source_compression, packed_info in rows:
                            if new_table and ts < self.started_at:
                                continue
                            if (typ & 0xffffffff) == 1:
                                memory_text = content
                                if isinstance(memory_text, bytes):
                                    memory_text = (bounded_decompress(memory_text) if compression else memory_text).decode('utf-8')
                                if isinstance(memory_text, str) and memory_text.strip():
                                    identity = hashlib.sha256(f'{account}|{contact}|{server_id or name+str(local_id)}'.encode()).hexdigest()
                                    memory_rows.append((identity, contact, 'out' if sender == own_id else 'in', ts, memory_text,'本人' if sender==own_id else self.names.get(users.get(sender,''),'群成员（未确认）' if contact.endswith('@chatroom') else '对方')))
                                    observed=IncomingMessage(external_id='local:'+hashlib.sha256(f'{account}|{contact}|{server_id or name+":"+str(local_id)}'.encode()).hexdigest(),contact=contact,
                                        sender='本人' if sender==own_id else self.names.get(users.get(sender,''),'对方'),content=memory_text,received_at=datetime.fromtimestamp(ts,timezone.utc),
                                        raw_summary=json.dumps({'source':name,'local_id':local_id,'server_id':server_id}))
                                    provenance=self.personal.observe(observed,'out' if sender==own_id else 'in')
                                    if provenance=='ai_generated':memory_rows[-1]=(*memory_rows[-1][:6],'本人（AI生成，仅作已发送上下文）')
                            if sender==own_id and (typ&0xffffffff) in {3,34,47}:
                                own_content=content
                                if isinstance(own_content,bytes):own_content=(bounded_decompress(own_content) if compression else own_content).decode('utf-8',errors='replace')
                                own_type={3:'image',34:'voice',47:'sticker'}[typ&0xffffffff]
                                own_meta={'source':name,'local_id':local_id,'server_id':server_id,'created_at':ts,'owner_message':True}
                                if own_type=='image':
                                    from app.media_images import image_refs
                                    own_meta['image_refs']=image_refs(packed_info,own_content)
                                if own_type=='sticker':
                                    try:
                                        from app.stickers import metadata
                                        from xml.etree import ElementTree as ET
                                        meta=metadata(own_content,require_url=False);node=ET.Element('msg');ET.SubElement(node,'emoji',{'md5':meta['md5'],'cdnurl':meta['url'],'attachedtext':meta['label']})
                                        own_meta['sticker_source']=ET.tostring(node,encoding='unicode')
                                    except ValueError:pass
                                self.own_media.append(IncomingMessage(external_id='local:'+hashlib.sha256(f'{account}|{contact}|{server_id or name+":"+str(local_id)}'.encode()).hexdigest(),
                                    contact=contact,sender='本人',content='本人发出的'+own_type,message_type=own_type,received_at=datetime.fromtimestamp(ts,timezone.utc),raw_summary=json.dumps(own_meta)))
                            if sender == own_id or local_id <= last_own_id or (typ & 0xffffffff) in {10000, 10002}:
                                continue
                            if isinstance(content, bytes):
                                content = (bounded_decompress(content)
                                           if compression else content).decode("utf-8")
                            if (typ & 0xffffffff) == 1 and (not isinstance(content, str) or not content.strip()):
                                continue
                            if not isinstance(content, str):
                                content = ""
                            if contact.endswith("@chatroom") and self.config.wechat.group_only_mentions:
                                if isinstance(source_text, bytes):
                                    source_text = (bounded_decompress(source_text, max_bytes=1024*1024)
                                                   if source_compression else source_text).decode("utf-8")
                                at_match = re.search(r"<atuserlist>(.*?)</atuserlist>", source_text or "", re.S)
                                addressed = (bool(at_match and (account in at_match[1] or "notify@all" in at_match[1]))
                                             or bool(self.self_display_name and "@" + self.self_display_name in content))
                                if not addressed:
                                    continue
                                prefix = users.get(sender, "") + ":\n"
                                if content.startswith(prefix):
                                    content = content[len(prefix):]
                            msg_type = "text" if (typ & 0xffffffff) == 1 else {
                                3: "image", 34: "voice", 43: "video", 47: "sticker", 49: "attachment"
                            }.get(typ & 0xffffffff, "unknown")
                            media_paths=[];sticker_meta={}
                            if msg_type=='image':
                                from app.media_images import image_refs
                                sticker_meta={'image_refs':image_refs(packed_info,content)}
                            if msg_type=='sticker':
                                try:
                                    if self.config.adapter=='history_http_sender' and self.config.media.images_enabled:
                                        from app.stickers import metadata
                                        from xml.etree import ElementTree as ET
                                        meta=metadata(content,require_url=False)
                                        source=ET.Element('msg');ET.SubElement(source,'emoji',{'md5':meta['md5'],'cdnurl':meta['url'],'attachedtext':meta['label']})
                                        sticker_meta={'sticker_md5':meta['md5'],'sticker_source':ET.tostring(source,encoding='unicode')}
                                        content='[表情包]'
                                    else:
                                        from app.stickers import acquire
                                        account_dirs={parent.parent for file_info in self.files.values()
                                                      for parent in Path(file_info['source']).parents if parent.name=='db_storage'}
                                        digest,media_paths,label=acquire(content,self.root,account_dirs)
                                        sticker_meta={'sticker_md5':digest,'asset_verified':True}
                                        content='[表情包]'+('\n'+label if label else '')
                                except Exception as exc:
                                    sticker_meta={'sticker_error':type(exc).__name__}
                            if msg_type != "text":
                                if not media_paths:content = {"image": "对方发来图片，内容尚未识别", "voice": "对方发来语音，尚未转写",
                                           "video": "对方发来视频，内容尚未识别"}.get(msg_type, "对方发来附件或非文字消息，内容尚未解析")
                            identity = f"{account}|{contact}|{server_id or name + ':' + str(local_id)}"
                            result.append(IncomingMessage(
                                external_id="local:" + hashlib.sha256(identity.encode()).hexdigest(),
                                contact=contact, sender=self.names.get(users.get(sender, contact), "联系人"), content=content,
                                sender_key=hashlib.sha256(users.get(sender,contact).encode()).hexdigest(),
                                message_type=msg_type,
                                media_paths=media_paths,
                                display_name=self.names.get(contact, contact),
                                chat_type="group" if contact.endswith("@chatroom") else "friend",
                                received_at=datetime.fromtimestamp(ts, timezone.utc)))
                            result[-1].raw_summary = json.dumps({"source": name, "local_id": local_id, "server_id":server_id,"created_at": ts,**sticker_meta})
                        self.cursors[cursor_key] = high
                    self.signatures[name] = observed
                finally:
                    conn.close()
        from app.chat_memory import remember
        remember(self.config.resolve(self.config.paths.chat_memory), memory_rows)
        return result

    def poll(self):
        cursors, signatures = self.cursors.copy(), self.signatures.copy()
        try:
            if any(signature(Path(info["source"])) != self.contact_signatures.get(name)
                   for name, info in self.keys.items() if name.replace("\\", "/").endswith("/contact/contact.db")):
                self._load_names()
            return self._read()
        except Exception:
            # A later shard can fail after earlier shards were read. Retry the
            # whole batch instead of silently dropping the earlier messages.
            self.cursors, self.signatures = cursors, signatures
            raise

    def send_text(self, contact, text):
        raise RuntimeError("当前为只读微信连接，不具备发送功能")
