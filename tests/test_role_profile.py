import json
import sqlite3
from app.role_profile import load, summarize
from app.style_history import examples


def test_role_has_only_current_contact_observations(tmp_path):
    path=tmp_path/'role.json'
    path.write_text(json.dumps({'global':{'sample_count':100},'contacts':{
        'one':{'style':{'sample_count':10},'situations':{'greeting':{'sample_count':4}}},
        'two':{'style':{'private':'other person story'}}}}),encoding='utf-8')
    result=load(path,'one','你好')
    assert result['same_contact_situation_habits']['sample_count']==4
    assert 'other person story' not in json.dumps(result)
    assert load(path,'unknown','你好')['current_contact_habits']=={}


def test_summary_does_not_export_stories_as_global_personality():
    result=summarize(['OK','好','私人的项目名字','没有，自己找'])
    assert '私人的项目名字' not in json.dumps(result,ensure_ascii=False)
    assert result['median_characters']==4.0
    assert result['evidence_level']=='limited'


def test_exact_old_reply_beats_recent_unrelated_rows(tmp_path):
    path=tmp_path/'samples.sqlite3'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE samples(contact TEXT,incoming TEXT,reply TEXT,created_at INTEGER)')
    conn.execute('INSERT INTO samples VALUES(?,?,?,?)',('one','给我发个照片','没有，自己搜一下吧',1))
    conn.executemany('INSERT INTO samples VALUES(?,?,?,?)',[('one','你好','你好',i) for i in range(2,405)])
    conn.commit();conn.close()
    assert examples(path,'one','给我发个照片',1)[0]['preferred_reply']=='没有，自己搜一下吧'
    assert examples(path,'one','进度怎么样了')==[]
