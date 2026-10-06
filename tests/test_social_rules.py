from app.social_rules import safe_social_reply


def test_only_noncommittal_social_replies_are_allowed():
    for text in ['咋了','好好好','啥愿望？','咋，真想我了？','哈哈哈','[捂脸]']:assert safe_social_reply(text)
    for text in ['我也想你','我同意','我明天过去','保证没问题','把银行卡发我','便宜一点也可以','我喜欢你']:assert not safe_social_reply(text)
