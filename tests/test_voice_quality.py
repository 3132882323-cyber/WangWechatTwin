from types import SimpleNamespace
from app.voice_quality import assess_transcript

def segment(prob=.95,ratio=1):
    return SimpleNamespace(avg_logprob=-.2,no_speech_prob=.1,compression_ratio=ratio,words=[SimpleNamespace(probability=prob)])

def test_unclear_words_repetition_and_any_numbers_require_review():
    assert not assess_transcript('我有点累',[segment()])
    assert assess_transcript('我到楼下了',[segment(.2)])
    assert assess_transcript('哈哈哈',[segment(ratio=3)])
    assert assess_transcript('明天3点送货',[segment()])
    assert assess_transcript('两百元',[segment()])
