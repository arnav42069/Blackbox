import asyncio
import base64
import json

import httpx
import pytest

from conftest import call


async def new(client):
    r=await client.post('/api/sessions', json={'model':'fixture:4b'})
    assert r.status_code==200, r.text
    return r.json()['id']


async def finish(app,sid):
    await app.state.agent.tasks[sid]
    return app.state.db.session(sid)


async def test_chat_tool_image_persistence(client,app,mock_ollama,png):
    sid=await new(client)
    mock_ollama.replies=[call('host.preflight'), {'role':'assistant','content':'Verified fixture host evidence'}]
    r=await client.post(f'/api/sessions/{sid}/messages', data={'text':'Check this screenshot and host tools'}, files={'images':('screen.png',png,'image/png')})
    assert r.status_code==200, r.text
    assert (await finish(app,sid))['status']=='completed'
    assert base64.b64decode(mock_ollama.vision_requests[0]['messages'][0]['images'][0])[:8]==b'\x89PNG\r\n\x1a\n'
    assert mock_ollama.requests[0]['tools']
    tool_message=mock_ollama.requests[1]['messages'][-1]
    assert tool_message['role']=='tool' and tool_message['tool_name']=='host.preflight'
    assert json.loads(tool_message['content'])['evidence'][0]['os']=='Linux'
    state=(await client.get(f'/api/sessions/{sid}')).json()
    assert state['messages'][-1]['content'].startswith('Verified fixture host evidence')
    aid=state['messages'][0]['attachment_ids'][0]
    img=await client.get(f'/api/sessions/{sid}/attachments/{aid}')
    assert img.status_code==200 and img.headers['content-type']=='image/png'
    sid2=await new(client)
    assert (await client.get(f'/api/sessions/{sid2}/attachments/{aid}')).status_code==404
    assert (await client.delete(f'/api/sessions/{sid}')).status_code==200
    assert not list((app.state.settings.data_dir/'attachments').iterdir())


async def test_auth_host_origin(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765') as c:
        assert (await c.get('/api/health')).status_code==200
        assert (await c.get('/api/sessions')).status_code==403
        assert (await c.get('/api/sessions',headers={'Origin':'http://evil.test'})).status_code==403
        assert (await c.get('/api/sessions',headers={'Origin':'http://127.0.0.1:8765'})).status_code==401
        assert (await c.get('/api/sessions/x/events',headers={'Origin':'http://127.0.0.1:8765'})).status_code==401
        assert (await c.get('/api/health',headers={'Host':'evil.test'})).status_code==403


async def test_pair_one_use(client,app):
    assert (await client.post('/api/pair',json={'token':'bad'})).status_code==401
    r=await client.get('/api/config',headers={'Origin':'http://evil.test'})
    assert r.status_code==403
    # Browser GETs cannot set the forbidden Origin header. Custom header is same-origin/CORS protected.
    headers=dict(client.headers)
    headers.pop('origin')
    headers['X-BlackBox-Origin']='http://127.0.0.1:8765'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765',headers=headers) as c:
        assert (await c.get('/api/config')).status_code==200


async def test_scope_denial_logged(client,app,mock_ollama):
    sid=await new(client)
    r=await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1/32'],'allowed_ports':'80'})
    assert r.status_code==200
    mock_ollama.replies=[call('web.inspect',url='http://8.8.8.8/')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Inspect target'})
    assert (await finish(app,sid))['status']=='denied'
    events=app.state.db.events(sid)
    assert any(e['type']=='tool_denied' for e in events)
    assert app.state.db.messages(sid)[-1]['content'].startswith('Action denied')


async def test_malformed_repair(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[{'content':'','tool_calls':[{'function':{'name':'host.preflight','arguments':'{invalid'}}]},call('host.preflight'),{'content':'repaired'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'preflight'})
    assert (await finish(app,sid))['status']=='completed'
    assert len(mock_ollama.requests)==3


async def test_no_vision_preserves_transaction(client,app,mock_ollama,png):
    mock_ollama.vision=False
    sid=await new(client)
    r=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Look'},files={'images':('a.png',png,'image/png')})
    assert r.status_code==400
    assert not app.state.db.messages(sid)
    assert not list((app.state.settings.data_dir/'attachments').iterdir())


async def test_bad_image_cleanup(client,app,png):
    sid=await new(client)
    r=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Look'},files=[('images',('good.png',png,'image/png')),('images',('bad.png',b'not image','image/png'))])
    assert r.status_code==400
    assert not app.state.db.messages(sid)
    assert not list((app.state.settings.data_dir/'attachments').iterdir())


async def test_manual_approval_and_cancel(client,app,mock_ollama):
    app.state.settings.prefs.mode='manual'
    sid=await new(client)
    mock_ollama.replies=[call('host.preflight'),{'content':'approved'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'preflight'})
    for _ in range(50):
        if app.state.agent.approvals:
            break
        await asyncio.sleep(.01)
    tid=next(iter(app.state.agent.approvals))
    assert (await client.post(f'/api/sessions/{sid}/messages',data={'text':'race'})).status_code==409
    assert (await client.post(f'/api/sessions/{sid}/approvals/{tid}',json={'approve':True})).status_code==200
    assert (await finish(app,sid))['status']=='completed'
    mock_ollama.replies=[call('host.preflight')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'preflight'})
    await asyncio.sleep(.02)
    assert (await client.post(f'/api/sessions/{sid}/cancel')).status_code==200
    assert app.state.db.session(sid)['status']=='cancelled'
    assert not app.state.agent.approvals


async def test_offline_is_actionable(client,app,mock_ollama):
    mock_ollama.unavailable=True
    assert (await client.get('/api/health')).status_code==200
    assert not (await client.get('/api/preflight')).json()['ollama']['connected']
    sid=await new(client)
    assert (await client.post(f'/api/sessions/{sid}/messages',data={'text':'hi'})).status_code==503


async def test_multiple_calls_and_budget(client,app,mock_ollama):
    sid=await new(client)
    app.state.settings.prefs.max_tool_calls=1
    mock_ollama.replies=[{'content':'','tool_calls':call('host.preflight')['tool_calls']*2}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Check twice'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==1
    assert any('budget exhausted' in e.get('message','') for e in app.state.db.events(sid))


async def test_two_concurrent_sends_only_start_one_run(client,app,mock_ollama):
    sid=await new(client)
    app.state.settings.prefs.mode='manual'
    mock_ollama.replies=[call('host.preflight')]
    results=await asyncio.gather(*[client.post(f'/api/sessions/{sid}/messages',data={'text':'preflight'}) for _ in range(2)])
    assert sorted(r.status_code for r in results)==[200,409]
    assert len(app.state.db.rows('SELECT * FROM runs'))==1
    await app.state.agent.cancel(sid)


async def test_invalid_scope_and_unknown_shell_are_denied(client,app,mock_ollama):
    sid=await new(client)
    r=await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':False,'allowed_targets':['127.0.0.1']})
    assert r.status_code==400
    mock_ollama.replies=[call('system.shell',command='id')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'test unknown tool'})
    assert (await finish(app,sid))['status']=='denied'
    assert 'Unregistered' in app.state.db.messages(sid)[-1]['content']


async def test_retry_reuses_images_under_current_policy(client,app,mock_ollama,png):
    sid=await new(client)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Look'},files={'images':('a.png',png,'image/png')})
    await finish(app,sid)
    first=app.state.db.messages(sid)[0]
    assert (await client.post(f'/api/sessions/{sid}/retry')).status_code==200
    assert (await finish(app,sid))['status']=='completed'
    users=[m for m in app.state.db.messages(sid) if m['role']=='user']
    assert len(users)==2 and users[-1]['attachment_ids']==first['attachment_ids']
    assert len(list((app.state.settings.data_dir/'attachments').iterdir()))==1


async def test_prompt_credentials_are_not_forwarded(client,app,mock_ollama):
    sid=await new(client)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Explain password=do-not-forward without using it'})
    await finish(app,sid)
    assert 'do-not-forward' not in mock_ollama.requests[0]['messages'][1]['content']


async def test_old_images_are_not_loaded_for_every_followup(client,app):
    from blackbox.storage.db import now,uid
    sid=await new(client)
    aid=uid()
    # A missing old file detects any eager read of discarded historical images.
    app.state.db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?)',(aid,sid,'/nonexistent/old-image.png','image/png','64x64',100,now()))
    app.state.db.message(sid,'user','Earlier image',attachments=[aid])
    app.state.db.message(sid,'assistant','Earlier image described')
    app.state.db.message(sid,'user','Text followup')
    history=await app.state.agent.history(sid)
    assert not any(m.get('images') or '_attachment_ids' in m for m in history)
