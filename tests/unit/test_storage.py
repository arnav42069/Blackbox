from pathlib import Path
from blackbox.storage.db import Database,now,uid
from blackbox.settings import Settings
from blackbox.storage.retention import prune


def test_restart_preserves_history_revokes_scope_and_interrupts_tools(tmp_path):
    db=Database(tmp_path/'test.db')
    sid=db.create_session('fixture:4b')['id']
    db.message(sid,'user','Persistent goal')
    rid,tid=uid(),uid()
    db.execute("UPDATE sessions SET status='running',scope_json=? WHERE id=?",('{"authorization_attested":true}',sid))
    db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,sid,now()))
    db.execute("INSERT INTO tool_events VALUES (?,?,?,'{}','{}',NULL,'running',?,NULL)",(tid,rid,'fixture',now()))
    db.event(sid,rid,'tool_started',{'tool_id':tid,'name':'fixture'})
    db.close()
    db=Database(tmp_path/'test.db')
    assert db.session(sid)['status']=='interrupted' and db.session(sid)['scope'] is None
    assert db.messages(sid)[0]['content']=='Persistent goal'
    assert db.one('SELECT status FROM tool_events')['status']=='interrupted'
    assert db.events(sid)[-1]['reason']=='interrupted'
    cursor=db.events(sid)[0]['event_id']
    assert all(e['event_id']>cursor for e in db.events(sid,cursor))
    assert tmp_path.joinpath('test.db').stat().st_mode & 0o777 == 0o600
    db.close()


def test_retention_and_orphan_cleanup(tmp_path):
    settings=Settings(data_dir=tmp_path/'data',config_dir=tmp_path/'config')
    settings.prepare()
    db=Database(settings.data_dir/'blackbox.db')
    sid=db.create_session('fixture')['id']
    db.execute('UPDATE sessions SET updated_at=0 WHERE id=?',(sid,))
    (settings.data_dir/'attachments'/'orphan.png').write_bytes(b'incomplete upload')
    prune(settings,db)
    assert db.session(sid) is None
    assert not list((settings.data_dir/'attachments').iterdir())
    db.close()


def test_diagnostic_connection_does_not_interrupt_running_database(tmp_path):
    db=Database(tmp_path/'test.db')
    sid=db.create_session('fixture')['id']
    rid=uid()
    db.execute("UPDATE sessions SET status='running',scope_json=? WHERE id=?", ('{"authorization_attested":true}',sid))
    db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,sid,now()))
    diagnostic=Database(tmp_path/'test.db',recover=False)
    assert diagnostic.session(sid)['status']=='running'
    assert diagnostic.session(sid)['scope']['authorization_attested']
    diagnostic.close()
    db.close()
