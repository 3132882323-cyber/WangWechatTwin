"""Optional second model drafts. Never sends or learns unapproved model output."""
import threading,time,json
from app.models import IncomingMessage,ContactProfile,RiskLevel
from app.risk import assess_risk
from app.prompts import PromptBuilder
from app.llm import ReplyLLM


class DeepseekCandidates:
    def __init__(self,config,db):
        self.config=config.model_copy(update={'mode':'shadow','openai':config.openai.model_copy(update={'provider':'deepseek_web'})})
        self.db=db;self.stop=threading.Event();self.wake=threading.Event();self.prompt=PromptBuilder(self.config);self.llm=None
        with db.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS candidate_jobs(id INTEGER PRIMARY KEY,contact TEXT,payload TEXT,status TEXT,at REAL)')
            connection.execute("UPDATE candidate_jobs SET status='pending' WHERE status='working'")
        self.thread=threading.Thread(target=self.run,daemon=True,name='deepseek-drafts');self.thread.start()

    def submit(self,messages):
        if not self.config.openai.deepseek_drafts:return
        from app.cli import merge_incoming_messages
        for message in messages:
            if message.message_type!='text' or assess_risk(message.content).level==RiskLevel.critical:continue
            with self.db.connect() as connection:
                row=connection.execute("SELECT id,payload FROM candidate_jobs WHERE contact=? AND status='pending' ORDER BY id DESC LIMIT 1",(message.contact,)).fetchone()
                if row:
                    message=merge_incoming_messages([IncomingMessage.model_validate_json(row[1]),message])[0]
                    connection.execute('UPDATE candidate_jobs SET payload=?,at=? WHERE id=?',(message.model_dump_json(),time.time(),row[0]))
                else:connection.execute("INSERT INTO candidate_jobs(contact,payload,status,at) VALUES(?,?,'pending',?)",(message.contact,message.model_dump_json(),time.time()))
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            if self.config.resolve(self.config.paths.pause_file).exists():self.stop.wait(.5);continue
            row=None
            try:
                with self.db.connect() as connection:
                    row=connection.execute("SELECT id,payload FROM candidate_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
                    if row:connection.execute("UPDATE candidate_jobs SET status='working' WHERE id=?",(row[0],))
                if not row:self.wake.wait(.5);self.wake.clear();continue
                message=IncomingMessage.model_validate_json(row[1]);risk=assess_risk(message.content)
                profile=ContactProfile(name=message.display_name or message.contact,relationship='以当前聊天为准',mode='shadow')
                payload=self.prompt.user_payload(message,profile,risk,self.db.recent_messages(message.contact,10),[],[])
                if not json.loads(payload).get('__media_paths'):
                    if self.llm is None:self.llm=ReplyLLM(self.config)
                    result=self.llm.decide(self.prompt.system_prompt(),payload,risk.level);result.action='review'
                    result.reason='DeepSeek 对照草稿，仅供保存和比较，不直接发送；'+result.reason
                    from app.personal_memory import PersonalMemory
                    PersonalMemory(self.config.resolve(self.config.paths.personal_database)).note(message.external_id,message.contact,'deepseek',result.reply+'；参考理由：'+result.reason)
                    self.db.create_draft(message.contact,None,result,kind='deepseek_candidate')
                with self.db.connect() as connection:connection.execute("UPDATE candidate_jobs SET status='done',payload='' WHERE id=?",(row[0],))
            except Exception:
                if row:
                    with self.db.connect() as connection:connection.execute("UPDATE candidate_jobs SET status='failed' WHERE id=?",(row[0],))
                self.db.add_event('deepseek_candidate_error','DeepSeek 对照草稿未完成，主回复继续运行')

    def close(self):self.stop.set();self.wake.set()
