import hashlib,io,json
from PIL import Image
import pytest
from app.config import AppConfig
from app.db import Database
from app.models import IncomingMessage,ReplyDecision,RiskLevel
from app.pipeline import ReplyPipeline
from app.sticker_catalog import item
from app.stickers import _frames


def setup_catalog(tmp_path):
    cfg=AppConfig(project_root=tmp_path,mode='low_risk_auto',adapter='mock')
    cfg.stickers.enabled=True
    assets=cfg.resolve(cfg.paths.history_reader)/'sticker_assets';assets.mkdir(parents=True)
    image=Image.new('RGB',(10,10),'red');buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue()
    digest=hashlib.md5(data).hexdigest();frames=_frames(data,digest,assets)
    catalog={'items':[{'id':digest,'label':'这是好事啊','approved':True,'tags':['celebrate'],
        'original':digest+'.png','preview':frames[0].name,'preview_sha256':hashlib.sha256(frames[0].read_bytes()).hexdigest()}]}
    cfg.resolve(cfg.stickers.catalog).write_text(json.dumps(catalog))
    db=Database(tmp_path/'state.sqlite3');return cfg,db,digest


def test_suggestion_stays_pending_and_does_not_repeat(tmp_path):
    cfg,db,digest=setup_catalog(tmp_path);pipe=ReplyPipeline(cfg,db)
    msg=IncomingMessage(contact='friend',sender='friend',external_id='one',content='我考过了')
    pipe._sticker_suggestion(msg,None);pipe._sticker_suggestion(msg,None)
    drafts=db.list_drafts(status='pending')
    assert len(drafts)==1 and drafts[0].kind=='sticker' and drafts[0].sticker_id==digest
    assert not db.approved_drafts()
    pipe._sticker_suggestion(msg.model_copy(update={'contact':'business','content':'合同签订成功了，准备付款'}),None)
    assert len(db.list_drafts())==1


def test_approval_routes_to_image_sender_not_text(tmp_path):
    cfg,db,digest=setup_catalog(tmp_path);pipe=ReplyPipeline(cfg,db)
    draft=db.create_draft('friend',None,ReplyDecision(action='review',risk=RiskLevel.low,reply='[表情图片]',sticker_id=digest),kind='sticker')
    class Adapter:
        sent=[]
        def send_text(self,*args):raise AssertionError('must not send placeholder as text')
        def send_sticker(self,contact,sticker):
            assert self.manual_approval_in_progress and self.approved_draft_id==draft
            self.sent.append((contact,sticker));return True
    adapter=Adapter()
    assert pipe.send_approved(adapter)==0
    db.update_draft(draft,'approved')
    assert pipe.send_approved(adapter)==1
    assert adapter.sent==[('friend',digest)] and not adapter.manual_approval_in_progress


def test_corrupt_asset_cannot_be_sent(tmp_path):
    cfg,db,digest=setup_catalog(tmp_path)
    entry=item(cfg,digest);entry['original_path'].write_bytes(b'not the selected sticker')
    with pytest.raises(ValueError):item(cfg,digest)
    with pytest.raises(ValueError):item(cfg,'../../private')
