import pytest
from app.voice import align_voice


def test_specific_voice_matches_unique_tail():
    rows = [('text','早上好'),('voice',''),('text','你看看')]
    assert align_voice(rows, [('text','旧记录')]+rows, 1) == 2


def test_ambiguous_voice_is_never_guessed():
    rows = [('text','早上好'),('voice',''),('voice',''),('text','你看看')]
    with pytest.raises(ValueError):
        align_voice(rows, rows, 1)
    with pytest.raises(ValueError):
        align_voice(rows, rows + [('text','更新消息')], 1)


def test_missing_text_anchors_is_rejected():
    with pytest.raises(ValueError):
        align_voice([('voice','')], [('voice','')], 0)
