import json
from app.llm import ReplyLLM
from app.models import ReplyDecision,RiskLevel


def test_hybrid_has_one_route_and_complex_or_visual_goes_to_gpt():
    calls=[]
    class Client:
        def __init__(self,name):self.name=name
        def decide(self,*args):calls.append(self.name);return ReplyDecision(action='review',risk=RiskLevel.low,reply='测试')
    router=ReplyLLM.__new__(ReplyLLM);router.hybrid={'chatgpt':Client('gpt'),'deepseek':Client('deepseek')}
    router.decide('system',json.dumps({'incoming':{'content':'你吃饭了吗'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'分析一下这个方案'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'看看图片'},'__media_paths':['pic.png']}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'付款怎么办'}}),RiskLevel.high)
    router.decide('system',json.dumps({'incoming':{'content':'早点休息吧','message_type':'text'},'transcription':{'source':'offline_whisper'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'[表情]','message_type':'sticker'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'[图片]','message_type':'image'}}),RiskLevel.low)
    assert calls==['deepseek','gpt','gpt','gpt','deepseek','gpt','gpt']
