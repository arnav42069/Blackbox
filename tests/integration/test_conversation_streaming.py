"""Deterministic conversation contract tests; no live model or remote targets."""
import asyncio
import json

import httpx
import pytest

from conftest import call
from test_api import new, finish
from blackbox.core.ollama_client import OllamaError


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, gate=None):
        self.chunks, self.gate = chunks, gate

    async def __aiter__(self):
        for i, chunk in enumerate(self.chunks):
            if i and self.gate:
                await self.gate.wait()
            yield (json.dumps(chunk) + '\n').encode()
            await asyncio.sleep(0)


async def install_stream(app, mock, chunks, gate=None):
    await app.state.ollama.http.aclose()
    def handler(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path == '/api/chat' and body.get('stream'):
            return httpx.Response(200, stream=Chunks(chunks, gate))
        return mock(request)
    app.state.ollama.http = httpx.AsyncClient(base_url='http://fixture', transport=httpx.MockTransport(handler))


def final_message(app, sid):
    return [m for m in app.state.db.messages(sid) if m['metadata'].get('conversation_kind') == 'final'][-1]


async def test_normal_conversation_no_tools(client, app, mock_ollama):
    sid = await new(client)
    mock_ollama.tools = False
    mock_ollama.replies = [{'content': 'Hello, how can I help?'}]
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Hello'})
    assert (await finish(app, sid))['status'] == 'completed'
    assert final_message(app, sid)['content'] == 'Hello, how can I help?'
    assert not app.state.db.rows('SELECT * FROM tool_events')


async def test_stream_is_persisted_before_completion_and_reload(client, app, mock_ollama):
    sid = await new(client)
    gate = asyncio.Event()
    await install_stream(app, mock_ollama, [
        {'message': {'thinking': 'PRIVATE', 'content': '<thi'}, 'done': False},
        {'message': {'content': 'nk>SECRET</think>Hello '}, 'done': False},
        {'message': {'content': '**lab**'}, 'done': False}, {'done': True}], gate)
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Hello'})
    gate.set()
    assert (await finish(app, sid))['status'] == 'completed'
    events = app.state.db.events(sid)
    deltas = [e for e in events if e['type'] == 'answer_delta']
    assert [e['content'] for e in deltas] == ['Hello ', 'Hello **lab**']
    assert len({e['message_id'] for e in deltas}) == 1
    assert 'SECRET' not in json.dumps(events) and 'PRIVATE' not in json.dumps(events)
    assert mock_ollama.review_requests[0]['stream'] is False
    assert events[-1]['type'] == 'run_finished'
    reloaded = (await client.get(f'/api/sessions/{sid}')).json()
    assert reloaded['status'] == 'completed'
    assert reloaded['messages'][-1]['id'] == deltas[0]['message_id']
    assert reloaded['messages'][-1]['content'] == 'Hello **lab**'


async def test_sequential_tools_then_final_answer(client, app, mock_ollama):
    sid = await new(client)
    mock_ollama.replies = [call('workspace.list'), call('workspace.list', path='.'), {'content': 'The workspace was listed; no vulnerabilities were verified.'}]
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'List workspace twice and explain'})
    assert (await finish(app, sid))['status'] == 'completed'
    assert len(app.state.db.rows('SELECT * FROM tool_events')) == 2
    answer = final_message(app, sid)['content']
    assert 'no vulnerabilities were verified' in answer
    assert 'Actual tool outcomes' in answer
    assert all(r['stream'] for r in mock_ollama.requests)


async def test_mixed_thinking_text_and_tools_survive_history(client,app,mock_ollama):
    sid=await new(client)
    first={**call('workspace.list'),'thinking':'PRIVATE_REASONING',
           'content':'I will list the workspace, then summarize the actual result.'}
    mock_ollama.replies=[first,{'thinking':'PRIVATE_FINAL_REASONING',
                              'content':'The workspace listing finished. No file contents were read.'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'List workspace and summarize the result'})
    assert (await finish(app,sid))['status']=='completed'
    stored=next(m for m in app.state.db.messages(sid) if m['metadata'].get('tool_calls'))
    assert stored['content']==first['content']
    history=await app.state.agent.history(sid)
    assert next(m for m in history if m.get('tool_calls'))['content']==first['content']
    assert any(m['role']=='tool' for m in mock_ollama.requests[1]['messages'])
    assert 'No file contents were read' in final_message(app,sid)['content']
    assert 'PRIVATE_' not in json.dumps(app.state.db.messages(sid)+app.state.db.events(sid))


async def test_thinking_only_completion_gets_one_final_answer_attempt(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[{'thinking':'PRIVATE_REASONING','content':''},
                         {'content':'Hello! I can explain security concepts and inspect authorized systems.'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello, what can you do?'})
    assert (await finish(app,sid))['status']=='completed'
    assert 'Hello!' in final_message(app,sid)['content']
    assert len(mock_ollama.requests)==2
    assert not mock_ollama.requests[1].get('tools')
    assert 'PRIVATE_REASONING' not in json.dumps(app.state.db.messages(sid)+app.state.db.events(sid))


async def test_empty_model_response_visible_error(client, app, mock_ollama):
    sid = await new(client)
    mock_ollama.replies = [{'content': ''}]
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Hello'})
    assert (await finish(app, sid))['status'] == 'failed'
    assert 'empty answer' in final_message(app, sid)['content']


async def test_repeated_thinking_only_reply_is_a_visible_bounded_failure(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[{'thinking':'Hidden','content':''}]*3
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(mock_ollama.requests)==2
    assert 'empty answer' in final_message(app,sid)['content']
    assert 'Hidden' not in json.dumps(app.state.db.events(sid))


async def test_tool_denial_has_conversational_explanation(client, app, mock_ollama):
    sid = await new(client)
    mock_ollama.replies = [call('web.inspect', url='http://192.0.2.1/')]
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Inspect target'})
    assert (await finish(app, sid))['status'] == 'denied'
    assert 'Action denied' in final_message(app, sid)['content']


async def test_timeout_retry_before_output(app, mock_ollama):
    attempts = []
    await app.state.ollama.http.aclose()
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadTimeout('fixture timeout')
        return httpx.Response(200, stream=Chunks([{'message': {'content': 'Recovered'}, 'done': True}]))
    app.state.ollama.http = httpx.AsyncClient(base_url='http://fixture', transport=httpx.MockTransport(handler))
    public = []
    response = await app.state.ollama.chat_stream('fixture', [], on_content=public.append)
    assert response['message']['content'] == 'Recovered' and len(attempts) == 2
    assert public == ['Recovered']


async def test_cancel_stream_retains_partial(client, app, mock_ollama):
    sid = await new(client)
    gate = asyncio.Event()
    await install_stream(app, mock_ollama, [{'message': {'content': 'Partial measured output'}, 'done': False}, {'done': True}], gate)
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Hello'})
    for _ in range(100):
        if any(e['type'] == 'answer_delta' for e in app.state.db.events(sid)):
            break
        await asyncio.sleep(.001)
    assert app.state.db.session(sid)['status'] == 'running'
    assert 'Partial measured output' in (await client.get(f'/api/sessions/{sid}')).text
    await client.post(f'/api/sessions/{sid}/cancel')
    assert app.state.db.session(sid)['status'] == 'cancelled'
    assert 'Partial measured output' in final_message(app, sid)['content']
    assert 'Stopped by user' in final_message(app, sid)['content']
    assert app.state.db.events(sid)[-1]['type'] == 'run_cancelled'


async def test_backend_silent_completion_is_failed(client, app, monkeypatch):
    sid = await new(client)
    from blackbox.core.goals import GoalController
    async def silent(*args):
        return 'completed'
    monkeypatch.setattr(GoalController, 'run', silent)
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Continue task'})
    assert (await finish(app, sid))['status'] == 'failed'
    assert 'without a final answer' in final_message(app, sid)['content']
    assert not any(e['type'] == 'run_finished' for e in app.state.db.events(sid))


async def test_budget_exhaustion_visible_checkpoint(client, app, mock_ollama):
    sid = await new(client)
    app.state.settings.prefs.max_turns = 1
    mock_ollama.replies = [call('workspace.list')]
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'List workspace and explain'})
    assert (await finish(app, sid))['status'] == 'failed'
    answer = final_message(app, sid)['content']
    assert 'budget exhausted' in answer and 'Recorded actions' in answer
    assert 'new bounded run' in answer
    assert any(e['type']=='run_checkpoint' for e in app.state.db.events(sid))


@pytest.mark.parametrize('chunks', [[], [{'message': {'content': 'Partial'}, 'done': False}], [{'message': {'tool_calls': None}, 'done': True}]])
async def test_malformed_or_truncated_stream_is_not_success(app, mock_ollama, chunks):
    await install_stream(app, mock_ollama, chunks)
    with pytest.raises(OllamaError):
        await app.state.ollama.chat_stream('fixture', [])


async def test_unsupported_tools_falls_back_to_conversation(client, app, mock_ollama):
    sid = await new(client)
    await app.state.ollama.http.aclose()
    def handler(request):
        body=json.loads(request.content) if request.content else {}
        if request.url.path=='/api/chat':
            if body.get('tools'):
                return httpx.Response(400,json={'error':'model does not support tools'})
            return httpx.Response(200,stream=Chunks([{'message':{'content':'Hello without tools'},'done':True}]))
        return mock_ollama(request)
    app.state.ollama.http=httpx.AsyncClient(base_url='http://fixture',transport=httpx.MockTransport(handler))
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    assert (await finish(app,sid))['status']=='completed'
    assert final_message(app,sid)['content']=='Hello without tools'
    assert not mock_ollama.review_requests


async def test_indexed_tool_argument_fragments_are_assembled(app,mock_ollama):
    await install_stream(app,mock_ollama,[
        {'message':{'content':'Checking the workspace.', 'tool_calls':[{'function':{'index':0,'name':'workspace.list','arguments':'{"path":'}}]},'done':False},
        {'message':{'tool_calls':[{'function':{'index':0,'arguments':'"."}'}}]},'done':False},
        {'done':True}])
    from blackbox.core.agent import parse_calls
    response=await app.state.ollama.chat_stream('fixture',[])
    assert parse_calls(response['message'])==[{'function':{'name':'workspace.list','arguments':{'path':'.'}}}]
    assert response['message']['content']=='Checking the workspace.'


async def test_terminal_event_observes_persisted_answer_and_status(client,app,monkeypatch):
    sid=await new(client)
    original=app.state.agent.event
    observed=[]
    def event(sid,rid,kind,**payload):
        if kind in {'run_finished','run_failed','run_cancelled'}:
            assert app.state.db.session(sid)['status']!='running'
            assert app.state.db.one('SELECT status FROM runs WHERE id=?',(rid,))['status']!='running'
            assert final_message(app,sid)['content'].strip()
            observed.append(kind)
        return original(sid,rid,kind,**payload)
    monkeypatch.setattr(app.state.agent,'event',event)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    await finish(app,sid)
    assert observed==['run_finished']


async def test_identical_successful_actions_cannot_loop(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('workspace.list')]*4
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'List workspace and explain'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==2
    assert 'identical action' in final_message(app,sid)['content']


async def test_failed_tool_can_recover_with_different_action(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('workspace.read',path='missing.txt'),call('workspace.list'),{'content':'The requested file was unavailable. The workspace listing succeeded; no file contents are claimed.'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read missing.txt or list workspace if unavailable'})
    assert (await finish(app,sid))['status']=='completed'
    assert [r['status'] for r in app.state.db.rows('SELECT status FROM tool_events ORDER BY started_at')]==['failed','success']
    assert 'unavailable' in final_message(app,sid)['content']


async def test_model_timeout_failure_is_visible(client,app,mock_ollama):
    sid=await new(client)
    await app.state.ollama.http.aclose()
    def handler(request):
        if request.url.path=='/api/chat':
            raise httpx.ReadTimeout('fixture timeout')
        return mock_ollama(request)
    app.state.ollama.http=httpx.AsyncClient(base_url='http://fixture',transport=httpx.MockTransport(handler))
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    assert (await finish(app,sid))['status']=='failed'
    assert 'timed out' in final_message(app,sid)['content']


async def test_think_option_rejection_retries_without_private_trace(app,mock_ollama):
    await app.state.ollama.http.aclose()
    requests=[]
    def handler(request):
        body=json.loads(request.content); requests.append(body)
        if 'think' in body:
            return httpx.Response(400,json={'error':'think is not supported'})
        return httpx.Response(200,stream=Chunks([{'message':{'thinking':'PRIVATE','content':'Visible'},'done':True}]))
    app.state.ollama.http=httpx.AsyncClient(base_url='http://fixture',transport=httpx.MockTransport(handler))
    public=[]
    assert (await app.state.ollama.chat_stream('fixture',[],on_content=public.append))['message']['content']=='Visible'
    assert len(requests)==2 and public==['Visible']


async def test_goal_budget_checkpoint_and_continuation_preserve_real_evidence(client,app,monkeypatch):
    from blackbox.tools.base import ToolResult
    from blackbox.core.scans import Finding
    from blackbox.core.goals import GoalController
    sid=await new(client)
    assert (await client.put(f'/api/sessions/{sid}/scope',json={
        'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'80',
        'allow_active_scanning':True})).status_code==200
    async def scan(ctx,args):
        return ToolResult(ok=True,summary='Mocked scoped service observation',evidence=[Finding(
            kind='port',target='127.0.0.1',port=80,protocol='tcp',state='open',service='http',url='http://127.0.0.1/').model_dump()])
    async def inspect(ctx,args):
        ctx.request()
        return ToolResult(ok=True,summary='Mocked scoped HTTP 200',stdout='THM{fixture_evidence}' if args.url.endswith('/flag.txt') else 'Lab index',
            evidence=[{'url':args.url,'status':200}])
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',scan)
    monkeypatch.setattr(app.state.agent.registry.specs['web.inspect'],'run',inspect)
    monkeypatch.setitem(app.state.agent.registry.runner.binaries,'nmap','/fixture/nmap')
    app.state.settings.prefs.max_tool_calls=1
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Solve the authorized lab at 127.0.0.1 and retrieve flag.txt'})
    assert (await finish(app,sid))['status']=='checkpointed'
    goal=GoalController.load(app.state.db,sid)
    assert goal.checkpoint['tool_calls']==1 and goal.pending.tool=='web.inspect'
    assert 'Continue task' in final_message(app,sid)['content']
    app.state.settings.prefs.max_tool_calls=3
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Continue task'})
    assert (await finish(app,sid))['status']=='completed'
    assert GoalController.load(app.state.db,sid).result['content']=='THM{fixture_evidence}'
    assert [t['tool_name'] for t in app.state.db.rows('SELECT tool_name FROM tool_events ORDER BY started_at')]==['net.scan_ports','web.inspect','web.inspect']
    assert 'Room completion is not independently verified' in final_message(app,sid)['content']


async def test_cancel_before_task_initialization_still_persists_blocker(app):
    from blackbox.storage.db import uid,now
    db=app.state.db
    sid=db.create_session('fixture:4b')['id'];rid=uid()
    db.message(sid,'user','Hello')
    db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,sid,now()))
    db.execute("UPDATE sessions SET status='running' WHERE id=?",(sid,))
    app.state.agent.start(sid,rid)
    await app.state.agent.cancel(sid)
    assert db.session(sid)['status']=='cancelled'
    assert final_message(app,sid)['content'].strip()
    assert db.events(sid)[-1]['type']=='run_cancelled'


def test_restart_preserves_partial_and_persists_interruption(tmp_path):
    from blackbox.storage.db import Database,uid,now
    path=tmp_path/'recover.db';db=Database(path)
    sid=db.create_session('fixture')['id'];rid=uid()
    db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,sid,now()))
    db.execute("UPDATE sessions SET status='running' WHERE id=?",(sid,))
    mid=db.message(sid,'assistant','Partial answer',metadata={'run_id':rid,'conversation_kind':'draft','state':'streaming'})
    db.close();db=Database(path)
    try:
        assert db.session(sid)['status']=='interrupted'
        assert db.messages(sid)[0]['id']==mid and db.messages(sid)[0]['content']=='Partial answer'
        assert db.messages(sid)[0]['metadata']['state']=='complete'
        assert 'Backend restarted' in db.messages(sid)[-1]['content']
        assert db.events(sid)[-1]['type']=='run_finished'
    finally:
        db.close()


async def test_event_reconnect_replays_only_events_after_cursor(client,app):
    sid=await new(client)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    await finish(app,sid)
    journal=app.state.db.events(sid);after=journal[1]['event_id']
    route=next(r for r in app.routes if getattr(r,'path','')=='/api/sessions/{sid}/events')
    class Request:
        headers={}
        checks=0
        async def is_disconnected(self):
            self.checks+=1
            return self.checks>1
    response=await route.endpoint(sid,Request(),after)
    frames=[frame async for frame in response.body_iterator]
    events=[json.loads(frame.split('data: ',1)[1]) for frame in frames if 'data: ' in frame]
    assert [e['event_id'] for e in events]==[e['event_id'] for e in journal[2:]]
    assert response.headers['cache-control']=='no-cache'


async def test_tool_only_null_content_is_not_malformed(app,mock_ollama):
    await install_stream(app,mock_ollama,[{'message':{'content':None,'tool_calls':call('workspace.list')['tool_calls']},'done':True}])
    response=await app.state.ollama.chat_stream('fixture',[])
    from blackbox.core.agent import parse_calls
    assert parse_calls(response['message'])==call('workspace.list')['tool_calls']
    assert response['message']['content']==''


async def test_revision_persisted_before_delta_and_thinking_never_replayed(client,app,mock_ollama,monkeypatch):
    sid=await new(client)
    seen=[]
    original=app.state.agent.event
    def event(sid,rid,kind,**payload):
        if kind=='answer_delta':
            row=app.state.db.one('SELECT content,metadata_json FROM messages WHERE id=?',(payload['message_id'],))
            assert row['content']==payload['content']
            assert json.loads(row['metadata_json'])['revision']==payload['revision']
            seen.append(payload['revision'])
        return original(sid,rid,kind,**payload)
    monkeypatch.setattr(app.state.agent,'event',event)
    await install_stream(app,mock_ollama,[{'message':{'thinking':'SECRET','content':'Hello '},'done':False},
        {'message':{'content':'there'},'done':False},{'done':True}])
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    await finish(app,sid)
    assert seen==[1,2]
    assert 'SECRET' not in json.dumps(app.state.db.messages(sid))
    events=app.state.db.events(sid)
    assert next(e for e in events if e['type']=='user_message')['message_id']==app.state.db.messages(sid)[0]['id']
    assert next(e for e in events if e['type']=='assistant_message_started')['message_id']==final_message(app,sid)['id']
    assert final_message(app,sid)['metadata']['revision']==3


async def test_stream_revisions_not_forwarded_as_ollama_message_fields(client,app,mock_ollama):
    sid=await new(client)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    await finish(app,sid)
    history=await app.state.agent.history(sid)
    assert all('revision' not in m and 'conversation_kind' not in m and 'run_id' not in m for m in history)


async def test_model_token_budget_is_not_silent_success(client,app,mock_ollama):
    sid=await new(client)
    await install_stream(app,mock_ollama,[{'message':{'content':'Measured partial response'},'done':False},
        {'done':True,'done_reason':'length'}])
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    assert (await finish(app,sid))['status']=='failed'
    final=final_message(app,sid)['content']
    assert 'Measured partial response' in final and 'output budget exhausted' in final
    assert any(e['type']=='run_checkpoint' for e in app.state.db.events(sid))
    assert not any(e['type']=='run_finished' and e.get('reason')=='completed' for e in app.state.db.events(sid))
