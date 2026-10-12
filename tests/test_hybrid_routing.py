import json
from app.config import AppConfig
from app.llm import ReplyLLM
from app.models import ReplyDecision,RiskLevel


def test_hybrid_has_one_route_and_complex_or_visual_goes_to_gpt(tmp_path):
    calls=[]
    class Client:
        def __init__(self,name):self.name=name
        def decide(self,*args):calls.append(self.name);return ReplyDecision(action='review',risk=RiskLevel.low,reply='测试')
    router=ReplyLLM.__new__(ReplyLLM);router.hybrid={'chatgpt':Client('gpt'),'deepseek':Client('deepseek'),'doubao':Client('doubao')}
    router.config = AppConfig(project_root=tmp_path, openai={'provider': 'hybrid_web'})
    router.decide('system',json.dumps({'incoming':{'content':'你吃饭了吗'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'分析一下这个方案'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'看看图片'},'__media_paths':['pic.png']}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'付款怎么办'}}),RiskLevel.high)
    router.decide('system',json.dumps({'incoming':{'content':'早点休息吧','message_type':'text'},'transcription':{'source':'offline_whisper'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'[表情]','message_type':'sticker'}}),RiskLevel.low)
    router.decide('system',json.dumps({'incoming':{'content':'[图片]','message_type':'image'}}),RiskLevel.low)
    assert calls==['deepseek','gpt','doubao','gpt','deepseek','doubao','doubao']


def test_gpt_is_reserved_for_professional_or_agitated_content():
    route=ReplyLLM.route
    payload=lambda text,kind='text':json.dumps({'incoming':{'content':text,'message_type':kind}})
    assert route(payload('我今天有点累'),RiskLevel.low)=='deepseek'
    assert route(payload('下午3点见'),RiskLevel.medium)=='deepseek'
    assert route(payload('看看这张表情','sticker'),RiskLevel.low)=='doubao'
    assert route(payload('分析这个施工方案'),RiskLevel.medium)=='chatgpt'
    assert route(payload('气死我了，你是不是有病'),RiskLevel.low)=='chatgpt'


def test_everyday_doubao_keeps_visual_and_gpt_routes():
    payload = lambda text, kind='text': json.dumps({'incoming': {'content': text, 'message_type': kind}})
    assert ReplyLLM.route(payload('你吃饭了吗'), RiskLevel.low, 'doubao') == 'doubao'
    voice = json.dumps({'incoming': {'content': '早点休息吧', 'message_type': 'text'},
                        'transcription': {'source': 'offline_whisper'}})
    assert ReplyLLM.route(voice, RiskLevel.low, 'doubao') == 'doubao'
    assert ReplyLLM.route(payload('[图片]', 'image'), RiskLevel.low, 'doubao') == 'doubao'
    assert ReplyLLM.route(payload('[表情]', 'sticker'), RiskLevel.low, 'doubao') == 'doubao'
    assert ReplyLLM.route(payload('分析这个施工方案'), RiskLevel.medium, 'doubao') == 'chatgpt'
    assert ReplyLLM.route(payload('气死我了，你是不是有病'), RiskLevel.low, 'doubao') == 'chatgpt'
    assert ReplyLLM.route(payload('合同金额改一下'), RiskLevel.high, 'doubao') == 'chatgpt'


def test_disabled_deepseek_is_not_constructed_or_used(tmp_path, monkeypatch):
    from app import local_proxy_llm, web_llm
    created = []
    called = []

    class Client:
        def __init__(self, lane):
            self.lane = lane

        def decide(self, *args):
            called.append(self.lane)
            return ReplyDecision(action='review', risk=RiskLevel.low, reply='测试', confidence=.9)

    def web_client(config):
        created.append(config.openai.provider)
        return Client('chatgpt')

    def proxy_client(config, lane):
        created.append(lane)
        return Client(lane)

    monkeypatch.setattr(web_llm, 'WebReplyLLM', web_client)
    monkeypatch.setattr(local_proxy_llm, 'LocalProxyReplyLLM', proxy_client)
    config = AppConfig(project_root=tmp_path, openai={
        'provider': 'hybrid_web', 'everyday_lane': 'doubao', 'disabled_lanes': ['deepseek'],
        'local_proxies': {
            'deepseek': {'enabled': False, 'base_url': 'http://127.0.0.1:19655/v1', 'api_key_file': 'private/deepseek.txt'},
            'doubao': {'enabled': True, 'base_url': 'http://127.0.0.1:19656/v1', 'api_key_file': 'private/doubao.txt'},
        },
    })
    router = ReplyLLM(config)
    assert created == ['web', 'doubao']
    assert set(router.hybrid) == {'chatgpt', 'doubao'}
    payloads = [
        {'incoming': {'content': '你吃饭了吗', 'message_type': 'text'}},
        {'incoming': {'content': '早点休息吧', 'message_type': 'text'}, 'transcription': {'source': 'offline_whisper'}},
        {'incoming': {'content': '[图片]', 'message_type': 'image'}},
        {'incoming': {'content': '分析这个施工方案', 'message_type': 'text'}},
        {'incoming': {'content': '气死我了，你是不是有病', 'message_type': 'text'}},
    ]
    for payload in payloads:
        router._decide_once('system', json.dumps(payload), RiskLevel.low)
    assert called == ['doubao', 'doubao', 'doubao', 'chatgpt', 'chatgpt']
