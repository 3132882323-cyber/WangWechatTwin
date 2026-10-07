"""One local owner database. Observations, AI output and corrections stay distinct."""
from pathlib import Path
from contextlib import closing
import sqlite3,json,hashlib,time,re
from app.risk import assess_risk
from app.models import RiskLevel


class PersonalMemory:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,contact_key TEXT NOT NULL,direction TEXT NOT NULL,
                    speaker TEXT,content TEXT NOT NULL,kind TEXT,at INTEGER,provenance TEXT,source TEXT,local_id INTEGER,metadata TEXT);
                CREATE INDEX IF NOT EXISTS personal_contact_time ON observations(contact_key,at DESC);
                CREATE TABLE IF NOT EXISTS corrections(target_id TEXT PRIMARY KEY,contact_key TEXT NOT NULL,original TEXT,corrected TEXT,at REAL);
                CREATE TABLE IF NOT EXISTS ai_receipts(source TEXT,local_id INTEGER,contact_key TEXT,body_hash TEXT,PRIMARY KEY(source,local_id));
                CREATE TABLE IF NOT EXISTS ai_intents(context TEXT PRIMARY KEY,contact_key TEXT,body_hash TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS model_notes(target_id TEXT,contact_key TEXT,provider TEXT,content TEXT,at REAL,PRIMARY KEY(target_id,provider));
            ''')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=20,isolation_level=None);db.execute('PRAGMA journal_mode=WAL');return db

    @staticmethod
    def contact_key(contact):return hashlib.sha256(contact.encode()).hexdigest()

    def observe(self,message,direction='in',provenance='wechat_original'):
        try:origin=json.loads(message.raw_summary or '{}')
        except ValueError:origin={}
        source=origin.get('source','');local_id=origin.get('local_id',0);key=self.contact_key(message.contact)
        safe_meta={k:origin[k] for k in ['server_id','audio_sha256','image_sha256','asr_uncertain','original_type'] if k in origin}
        if message.media_paths:safe_meta['media_paths']=message.media_paths[:3]
        with closing(self.connect()) as db:
            receipt=db.execute('SELECT 1 FROM ai_receipts WHERE source=? AND local_id=?',(source,local_id)).fetchone()
            intent=db.execute('SELECT 1 FROM ai_intents WHERE contact_key=? AND body_hash=? AND created BETWEEN ? AND ?',(key,hashlib.sha256(message.content.encode()).hexdigest(),message.received_at.timestamp()-120,message.received_at.timestamp()+30)).fetchone()
            if direction=='out' and (receipt or intent):provenance='ai_generated'
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (message.external_id,key,direction,'本人' if direction=='out' else message.sender,message.content,message.message_type,
                 int(message.received_at.timestamp()),provenance,source,local_id,json.dumps(safe_meta)))
        return provenance

    def receipt(self,contact,text,source,local_id):
        with closing(self.connect()) as db:
            db.execute('INSERT OR REPLACE INTO ai_receipts VALUES(?,?,?,?)',(source,local_id,self.contact_key(contact),hashlib.sha256(text.encode()).hexdigest()))
            db.execute("UPDATE observations SET provenance='ai_generated' WHERE source=? AND local_id=?",(source,local_id))

    def import_history(self,path):
        if not Path(path).exists():return 0
        with closing(self.connect()) as dest:
            if dest.execute("SELECT 1 FROM metadata WHERE key='history_imported'").fetchone():return 0
            with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as source:
                rows=source.execute('SELECT id,contact,direction,speaker,content,created_at FROM history').fetchall()
            values=[(rid,self.contact_key(contact),direction,speaker,text,'text',at,'historical_unverified_origin','',0,'{}') for rid,contact,direction,speaker,text,at in rows]
            dest.execute('BEGIN IMMEDIATE');dest.executemany('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',values)
            dest.execute("INSERT INTO metadata VALUES('history_imported',?)",(str(time.time()),));dest.execute('COMMIT')
            return len(values)

    def recent(self,contact,limit=16,max_chars=3000):
        with closing(self.connect()) as db:
            rows=db.execute('SELECT id,direction,speaker,content,kind,at,provenance,metadata FROM observations WHERE contact_key=? ORDER BY at DESC,rowid DESC LIMIT ?',(self.contact_key(contact),limit)).fetchall()
        output=[];used=0
        for rid,direction,speaker,text,kind,at,provenance,metadata in rows:
            if assess_risk(text).level==RiskLevel.critical:continue
            text=text[:800]
            if used+len(text)>max_chars:continue
            output.append({'speaker':speaker,'direction':direction,'content':text,'kind':kind,'at':at,'provenance':provenance,'evidence_id':rid[:16],'media_paths':json.loads(metadata or '{}').get('media_paths',[])});used+=len(text)
        return list(reversed(output))

    def observed(self,identity):
        with closing(self.connect()) as db:return db.execute('SELECT 1 FROM observations WHERE id=?',(identity,)).fetchone() is not None

    def correct(self,identity,contact,original,corrected):
        with closing(self.connect()) as db:db.execute('INSERT OR REPLACE INTO corrections VALUES(?,?,?,?,?)',(identity,self.contact_key(contact),original,corrected,time.time()))

    def corrected(self,identity,contact):
        with closing(self.connect()) as db:
            row=db.execute('SELECT corrected FROM corrections WHERE target_id=? AND contact_key=?',(identity,self.contact_key(contact))).fetchone()
        return row[0] if row else None

    def note(self,identity,contact,provider,content):
        with closing(self.connect()) as db:db.execute('INSERT OR REPLACE INTO model_notes VALUES(?,?,?,?,?)',(identity,self.contact_key(contact),provider,content[:1200],time.time()))

    def notes(self,identities,contact):
        identities=[identity for identity in identities[:100] if isinstance(identity,str)]
        if not identities:return []
        with closing(self.connect()) as db:
            return [{'provider':provider,'content':content,'at':at} for provider,content,at in db.execute(
                'SELECT provider,content,at FROM model_notes WHERE contact_key=? AND target_id IN ('+','.join('?' for _ in identities)+') AND at>? ORDER BY at DESC LIMIT 2',
                (self.contact_key(contact),*identities,time.time()-600))]

    def human_style_examples(self,contact,incoming,limit=4):
        from app.style_history import grams
        wanted=grams(incoming)
        with closing(self.connect()) as db:
            rows=db.execute("SELECT id,direction,content,at,provenance FROM observations WHERE contact_key=? AND kind='text' ORDER BY at DESC,rowid DESC LIMIT 300",(self.contact_key(contact),)).fetchall()
        rows.reverse();previous=None;scored=[]
        for identity,direction,text,at,provenance in rows:
            if direction=='in':previous=(text,at);continue
            if provenance!='wechat_original' or not previous or at-previous[1]>1800:continue
            if assess_risk(text).level!=RiskLevel.low or assess_risk(previous[0]).level!=RiskLevel.low:continue
            if re.search(r'https?://|\d{7,}',text+previous[0]):continue
            similarity=len(wanted & grams(previous[0]))/max(1,len(wanted | grams(previous[0])))
            if similarity==0:continue
            scored.append((similarity,at,{'scenario':'已核验本人手发的当前联系人原话，仅模仿表达','incoming':previous[0],'preferred_reply':text,'evidence_id':identity,'provenance':provenance}))
        scored.sort(key=lambda row:(row[0],row[1]),reverse=True)
        return [row[2] for row in scored[:limit]]

    def voice_vocabulary(self,contact,owner_name,display_name=''):
        with closing(self.connect()) as db:
            rows=db.execute('SELECT corrected FROM corrections WHERE contact_key=? ORDER BY at DESC LIMIT 12',(self.contact_key(contact),)).fetchall()
        names=[owner_name,display_name]
        # Corrections supply local hints only; never force corrected words into an unrelated transcript.
        for row in rows:names.extend(re.findall(r'[\u4e00-\u9fff]{2,6}',row[0])[:5])
        return '，'.join(dict.fromkeys(name for name in names if name and not re.search(r'\d|https?://',name)))[:220]

    def exclude_ai_style(self,contact,examples):
        with closing(self.connect()) as db:
            ai={row[0] for row in db.execute("SELECT content FROM observations WHERE contact_key=? AND provenance='ai_generated'",(self.contact_key(contact),))}
        return [example for example in examples if example.get('preferred_reply') not in ai]

    def sending_intent(self,contact,text,context):
        with closing(self.connect()) as db:db.execute('INSERT OR IGNORE INTO ai_intents VALUES(?,?,?,?)',(context,self.contact_key(contact),hashlib.sha256(text.encode()).hexdigest(),time.time()))
