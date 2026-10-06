import sqlite3
from app.greeting_cache import lookup


def test_only_exact_same_contact_factual_free_greeting(tmp_path):
    path=tmp_path/'samples.sqlite3';db=sqlite3.connect(path)
    db.execute('CREATE TABLE samples(contact TEXT,incoming TEXT,reply TEXT,created_at INTEGER)')
    db.executemany('INSERT INTO samples VALUES(?,?,?,?)',[('a','在吗？','咋了',1),('b','在吗','我马上过去',2),('b','你好','你好',3)])
    db.commit();db.close()
    assert lookup(path,'a','在吗')=='咋了'
    assert lookup(path,'b','在吗') is None
    assert lookup(path,'unknown','在吗') is None
    assert lookup(path,'a','在吗，帮我转账') is None


def test_only_dominant_closed_owner_opener_can_generalize(tmp_path):
    import json
    profile=tmp_path/'role.json';profile.write_text(json.dumps({'global':{'common_conversation_openers':[['咋了',62],['你说',9]]}}),encoding='utf-8')
    assert lookup(tmp_path/'missing.sqlite','new','在吗',profile)=='咋了'
    assert lookup(tmp_path/'missing.sqlite','new','想你了',profile) is None
    assert lookup(tmp_path/'missing.sqlite','new','在吗，我明天来',profile) is None
