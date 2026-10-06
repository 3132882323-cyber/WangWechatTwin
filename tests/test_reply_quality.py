from app.reply_quality import laughter_only,trim_laughter,substantive_examples


def test_laughter_does_not_swallow_real_questions_or_content():
    for text in ['哈哈哈哈…','😂🤣','哈哈[笑哭]','笑死我了']:assert laughter_only(text)
    for text in ['哈？','哈哈，我昨天遇到个事','这个事你怎么看？']:assert not laughter_only(text)
    assert trim_laughter('哈哈哈，你先把具体情况发我，哈哈')=='你先把具体情况发我'


def test_filter_preserves_original_records():
    original={'samples':[{'reply':'哈哈哈'},{'reply':'你先把具体情况说一下'}],'common':[['哈哈',99],['咋了',20]]}
    result=substantive_examples(original)
    assert len(original['samples'])==2
    assert result['samples']==[{'reply':'你先把具体情况说一下'}]
    assert result['common']==[['咋了',20]]
