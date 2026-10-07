"""One transient page failure leaves a draft; repeated failure pauses processing."""
import time


def failed(config, db, stage):
    previous=db.get_state('browser_failures') or {}
    now=time.time()
    count=previous.get('consecutive',0)+1 if now-previous.get('last_at',0)<600 else 1
    stage=stage if stage in {'load','setup','reply','complete','queue'} else 'unknown'
    db.set_state('browser_failures',{'consecutive':count,'last_at':now,'stage':stage})
    paused=count>=3
    isolated=paused and config.openai.provider=='hybrid_web'
    if isolated:db.set_state('browser_failures',{'consecutive':count,'last_at':now,'stage':stage,'blocked_until':now+60,'lane':'chatgpt'})
    if paused and not isolated:
        target=config.resolve(config.paths.pause_file)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.touch()
    db.add_event('llm_error',f'网页阶段 {stage} 失败；'+('GPT 通道连续失败，暂缓该通道；其他模型继续' if isolated else '连续三次失败，已暂停' if paused else '本条留待审核，其他联系人继续处理')+'；未切回 Codex')
    return paused


def succeeded(db):
    db.set_state('browser_failures',{'consecutive':0,'last_at':time.time()})
