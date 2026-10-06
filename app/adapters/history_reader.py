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
    wal = wal_source.read_bytes() if wal_source.exists() else b""
    if signature(source) != before:
        raise SnapshotBusyError("微信记录正在更新，稍后重试读取")
    key, salt = bytes.fromhex(info["enc_key"]), bytes.fromhex(info["salt"])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3a for v in salt), 2, dklen=32)

    def decode(page, pg):
        payload = page[16 if pg == 1 else 0:4032]
        expected = hmac.new(mac_key, payload + struct.pack("<I", pg), hashlib.sha512).digest()
        if len(page) != 4096 or not hmac.compare_digest(expected, page[4032:]):
            raise RuntimeError("微信数据库认证失败，停止本次读取")
        return _decrypt_page(key, page, pg)

    with encrypted.open("rb") as src, target.open("wb") as dst:
        pg = 0
        while page := src.read(4096):
            pg += 1
            dst.write(decode(page, pg))
    if len(wal) >= 32:
        magic, version, page_size, seq, s1, s2, c1, c2 = struct.unpack(">8I", wal[:32])
        if magic not in (0x377f0682, 0x377f0683) or page_size != 4096:
            raise RuntimeError("微信日志格式尚不支持")
        endian = "<" if magic == 0x377f0682 else ">"
        state = wal_checksum(wal[:24], endian=endian)
        if state != (c1, c2):
            raise RuntimeError("微信日志头校验失败")
        frames, commit_end, db_size = [], 0, 0
        for offset in range(32, len(wal) - 4119, 4120):
            header = wal[offset:offset + 24]
            pg, size, fs1, fs2, fc1, fc2 = struct.unpack(">6I", header)
            page = wal[offset + 24:offset + 4120]
            if (fs1, fs2) != (s1, s2) or pg == 0:
                break
            expected = wal_checksum(header[:8] + page, state, endian)
            if expected != (fc1, fc2):
                break
            state = expected
            frames.append((pg, decode(page, pg)))
            if size:
                commit_end, db_size = len(frames), size
        with target.open("r+b") as dst:
            for pg, page in frames[:commit_end]:
                dst.seek((pg - 1) * 4096)
                dst.write(page)
            if commit_end:
                dst.truncate(db_size * 4096)
    connection = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("微信数据库副本完整性检查失败")
    finally:
        connection.close()
    return target, before


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
                        rows = conn.execute(
                            f'SELECT local_id,server_id,local_type,real_sender_id,create_time,message_content,'
                            f'WCDB_CT_message_content,{source_cols},{packed_col} FROM "{table}" WHERE local_id>? ORDER BY local_id',
                            (previous,))
                        rows = list(rows)
                        last_own_id = max((r[0] for r in rows if r[3] == own_id), default=previous)
                        for local_id, server_id, typ, sender, ts, content, compression, source_text, source_compression, packed_info in rows:
                            if new_table and ts < self.started_at:
                                continue
                            if (typ & 0xffffffff) == 1:
                                memory_text = content
                                if isinstance(memory_text, bytes):
                                    memory_text = (zstandard.ZstdDecompressor().decompress(memory_text, max_output_size=8*1024*1024) if compression else memory_text).decode('utf-8')
                                if isinstance(memory_text, str) and memory_text.strip():
                                    identity = hashlib.sha256(f'{account}|{contact}|{server_id or name+str(local_id)}'.encode()).hexdigest()
                                    memory_rows.append((identity, contact, 'out' if sender == own_id else 'in', ts, memory_text,'本人' if sender==own_id else self.names.get(users.get(sender,''),'群成员（未确认）' if contact.endswith('@chatroom') else '对方')))
                            if sender == own_id or local_id <= last_own_id or (typ & 0xffffffff) in {10000, 10002}:
                                continue
                            if isinstance(content, bytes):
                                content = (zstandard.ZstdDecompressor().decompress(content, max_output_size=8*1024*1024)
                                           if compression else content).decode("utf-8")
                            if (typ & 0xffffffff) == 1 and (not isinstance(content, str) or not content.strip()):
                                continue
                            if not isinstance(content, str):
                                content = ""
                            if contact.endswith("@chatroom") and self.config.wechat.group_only_mentions:
                                if isinstance(source_text, bytes):
                                    source_text = (zstandard.ZstdDecompressor().decompress(source_text, max_output_size=1024*1024)
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
