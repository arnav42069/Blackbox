"""Goal-controller regressions: mocked discovery, real loopback HTTP, no public labs."""
import asyncio
import json

import pytest
import pytest_asyncio

from blackbox.core.goals import GoalController
from blackbox.core.scans import ScanStore, Finding
from blackbox.storage.db import Database
from blackbox.tools.base import ToolResult
from test_api import new, finish
from conftest import call
from test_tools import web_server


@pytest_asyncio.fixture
async def flag_server():
    requests=[]
    async def handler(reader,writer):
        try:
            data=await reader.readuntil(b'\r\n\r\n')
            path=data.split(b' ')[1].decode()
            requests.append(path)
            status=200 if path in {'/','/flag.txt'} else 404
            body=b'THM{independent_local_evidence}' if path=='/flag.txt' else b'<h1>local fixture</h1>'
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    yield server.sockets[0].getsockname()[1],requests
    server.close()
    await server.wait_closed()


async def setup_goal(client,app,port,monkeypatch,**scope):
    sid=await new(client)
    authorization={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(port),'allow_active_scanning':True,**scope}
    assert (await client.put(f'/api/sessions/{sid}/scope',json=authorization)).status_code==200
    scans=[]
    async def scan(ctx,args):
        scans.append(args.model_dump())
        return ToolResult(ok=True,exit_code=0,evidence=[Finding(kind='port',target='127.0.0.1',port=port,protocol='tcp',service='http',state='open',url=f'http://127.0.0.1:{port}/').model_dump()],summary='Mocked Nmap service discovery')
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',scan)
    monkeypatch.setitem(app.state.agent.registry.runner.binaries,'nmap','/fixture/nmap')
    return sid,scans


async def send(client,app,sid,text='Solve the authorized TryHackMe room at 127.0.0.1 and retrieve flag.txt'):
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':text})
    assert response.status_code==200,response.text
    return await finish(app,sid)


async def test_premature_ssh_request_is_replaced_by_discovery(client,app,mock_ollama,flag_server,monkeypatch):
    port,requests=flag_server
    sid,scans=await setup_goal(client,app,port,monkeypatch)
    # This was the model response that previously ended the entire task immediately.
    mock_ollama.replies=[call('ssh.probe',target='127.0.0.1',profile='absent')]
    assert (await send(client,app,sid))['status']=='completed'
    tools=app.state.db.rows('SELECT tool_name,status FROM tool_events ORDER BY started_at')
    assert [t['tool_name'] for t in tools]==['net.scan_ports','web.inspect','web.inspect']
    assert scans and requests==['/','/flag.txt']
    assert not mock_ollama.requests
    state=GoalController.load(app.state.db,sid)
    assert state.status=='completed' and state.result['content']=='THM{independent_local_evidence}'
    assert state.result['tool_id'] and state.hypotheses and state.decomposition
    assert 'Room completion is not independently verified' in app.state.db.messages(sid)[-1]['content']


async def test_existing_http_evidence_skips_nmap(client,app,flag_server,monkeypatch):
    port,_=flag_server
    sid,scans=await setup_goal(client,app,port,monkeypatch)
    ScanStore(app.state.db).save(sid,app.state.db.session(sid)['scope'],'net.scan_ports',{},
        [Finding(kind='port',target='127.0.0.1',port=port,protocol='tcp',state='open',url=f'http://127.0.0.1:{port}/')])
    assert (await send(client,app,sid))['status']=='completed'
    assert not scans


async def test_tool_budget_checkpoint_and_explicit_continuation(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,scans=await setup_goal(client,app,port,monkeypatch)
    app.state.settings.prefs.max_tool_calls=1
    assert (await send(client,app,sid))['status']=='checkpointed'
    state=GoalController.load(app.state.db,sid)
    goal_id=state.id
    assert state.pending.tool=='web.inspect' and state.checkpoint['tool_calls']==1
    assert not requests
    assert (await send(client,app,sid,'Continue task'))['status']=='checkpointed'
    assert requests==['/'] and len(scans)==1
    assert (await send(client,app,sid,'Continue task'))['status']=='completed'
    assert requests==['/','/flag.txt'] and len(scans)==1
    assert GoalController.load(app.state.db,sid).id==goal_id
    assert not app.state.agent.active(sid)


async def test_request_budget_checkpoint(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch,max_requests_per_run=1)
    assert (await send(client,app,sid))['status']=='checkpointed'
    assert requests==['/']
    assert (await send(client,app,sid,'Resume goal'))['status']=='completed'
    assert requests==['/','/flag.txt']


async def test_out_of_scope_never_executes_goal(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,scans=await setup_goal(client,app,port,monkeypatch)
    assert (await send(client,app,sid,'Solve TryHackMe at 192.0.2.1 and retrieve flag.txt'))['status']=='denied'
    assert not scans and not requests and not app.state.db.rows('SELECT * FROM tool_events')


async def test_scan_failure_does_not_fabricate_access_or_flag(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    async def fail(*args):
        return ToolResult(ok=False,summary='fixture scanner failed',exit_code=2)
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',fail)
    assert (await send(client,app,sid))['status']=='needs_access'
    goal=GoalController.load(app.state.db,sid)
    assert not goal.result and not requests
    assert any('scanner failed' in l for l in goal.limitations)
    assert any('SSH only' in l for l in goal.limitations)
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==1


async def test_cancel_persists_checkpoint_and_no_auto_resume(client,app,flag_server,monkeypatch):
    port,_=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    started=asyncio.Event()
    async def slow(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',slow)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Solve TryHackMe at 127.0.0.1 and retrieve flag.txt'})
    await asyncio.wait_for(started.wait(),2)
    await app.state.agent.cancel(sid)
    assert app.state.db.session(sid)['status']=='cancelled'
    goal=GoalController.load(app.state.db,sid)
    assert goal.status=='cancelled' and goal.pending.tool=='net.scan_ports'
    assert not app.state.agent.active(sid)
    # The goal survives a reopened database, independently of Agent.plans memory.
    copy=Database(app.state.db.path,recover=False)
    try:
        assert GoalController.load(copy,sid).id==goal.id
    finally:
        copy.close()


async def test_wall_timeout_checkpoint(client,app,flag_server,monkeypatch):
    port,_=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    app.state.settings.prefs.max_wall_seconds=0.05
    async def slow(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',slow)
    assert (await send(client,app,sid))['status']=='checkpointed'
    assert 'wall-time' in GoalController.load(app.state.db,sid).checkpoint['reason']


async def test_ssh_access_failure_is_tool_local_and_other_discovery_continues(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    monkeypatch.setattr('blackbox.tools.ssh.public_profiles',lambda settings:[{'name':'fixture','host':'127.0.0.1','port':22,'read_roots':['/home']}])
    async def no_http(ctx,args):
        return ToolResult(ok=True,summary='No HTTP identified',evidence=[{'kind':'host','target':'127.0.0.1'}])
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',no_http)
    async def missing_access(ctx,args):
        # Another previously captured observation becomes available during this turn.
        ScanStore(ctx.db).save(sid,ctx.scope.data,'vision.observe',{},
            [Finding(kind='port',target='127.0.0.1',port=port,state='open',url=f'http://127.0.0.1:{port}/')],
            source='screenshot',status='observed',complete=False)
        return ToolResult(ok=False,summary='Needs access: no configured SSH key or SSH agent')
    monkeypatch.setattr(app.state.agent.registry.specs['ssh.find_files'],'run',missing_access)
    assert (await send(client,app,sid))['status']=='completed'
    names=[t['tool_name'] for t in app.state.db.rows('SELECT tool_name FROM tool_events ORDER BY started_at')]
    assert names==['net.scan_ports','ssh.find_files','web.inspect','web.inspect']
    goal=GoalController.load(app.state.db,sid)
    assert any('Needs access' in l for l in goal.limitations)
    assert goal.result and requests==['/','/flag.txt']


async def test_soft_404_is_not_a_flag_and_step_budget_saves_next_action(client,app,monkeypatch,web_server):
    port,requests=web_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    app.state.settings.prefs.max_turns=3
    assert (await send(client,app,sid))['status']=='checkpointed'
    goal=GoalController.load(app.state.db,sid)
    assert not goal.result and goal.pending.tool=='web.discover_paths'
    assert requests==['/','/flag.txt']
    assert 'verified tool provenance' not in app.state.db.messages(sid)[-1]['content']


async def test_explicit_url_uses_public_http_without_scan_or_ssh(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,scans=await setup_goal(client,app,port,monkeypatch,allow_active_scanning=False)
    assert (await send(client,app,sid,f'Retrieve flag.txt from http://127.0.0.1:{port}/'))['status']=='completed'
    assert not scans and requests==['/','/flag.txt']


async def test_scope_revocation_blocks_checkpoint_continuation(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    app.state.settings.prefs.max_tool_calls=1
    assert (await send(client,app,sid))['status']=='checkpointed'
    app.state.db.execute('UPDATE sessions SET scope_json=NULL WHERE id=?',(sid,))
    assert (await send(client,app,sid,'Continue task'))['status']=='denied'
    assert not requests


async def test_content_discovery_continues_after_missing_direct_flag(client,app,monkeypatch):
    requests=[]
    async def handler(reader,writer):
        try:
            data=await reader.readuntil(b'\r\n\r\n')
            path=data.split(b' ')[1].decode()
            requests.append(path)
            status=200 if path in {'/','/docs','/docs/flag.txt'} else 404
            body=b'THM{nested_local_file}' if path=='/docs/flag.txt' else b'local fixture'
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    try:
        port=server.sockets[0].getsockname()[1]
        sid,_=await setup_goal(client,app,port,monkeypatch)
        monkeypatch.setitem(app.state.agent.registry.runner.binaries,'ffuf',None)
        app.state.settings.prefs.http_rate=5
        assert (await send(client,app,sid))['status']=='completed'
        names=[t['tool_name'] for t in app.state.db.rows('SELECT tool_name FROM tool_events ORDER BY started_at')]
        assert names==['net.scan_ports','web.inspect','web.inspect','web.discover_paths','web.inspect']
        assert '/docs/flag.txt' in requests
        assert GoalController.load(app.state.db,sid).result['content']=='THM{nested_local_file}'
    finally:
        server.close()
        await server.wait_closed()


async def test_restart_marks_active_goal_interrupted_and_revokes_scope(client,app,flag_server,monkeypatch):
    from blackbox.core.goals import TaskGoal,Action
    port,_=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    goal=TaskGoal(instruction='Solve room',target='127.0.0.1',pending=Action(tool='web.inspect',arguments={'url':f'http://127.0.0.1:{port}/'},rationale='pending read'))
    app.state.db.execute('INSERT INTO task_goals VALUES (?,?,?)',(sid,goal.model_dump_json(),0))
    copy=Database(app.state.db.path,recover=True)
    try:
        restored=GoalController.load(copy,sid)
        assert restored.status=='interrupted' and restored.pending.tool=='web.inspect'
        assert copy.session(sid)['scope'] is None
        assert 'explicit continuation' in restored.checkpoint['reason']
        assert not app.state.agent.active(sid)
    finally:
        copy.close()


async def test_multiple_known_http_services_are_investigated_without_ssh(client,app,flag_server,web_server,monkeypatch):
    flag_port,flag_requests=flag_server
    soft_port,soft_requests=web_server
    sid,scans=await setup_goal(client,app,flag_port,monkeypatch,allowed_ports=f'{flag_port},{soft_port}',allow_active_scanning=False)
    store=ScanStore(app.state.db)
    for port in [flag_port,soft_port]:
        store.save(sid,app.state.db.session(sid)['scope'],'vision.observe',{},
            [Finding(kind='port',target='127.0.0.1',port=port,state='open',url=f'http://127.0.0.1:{port}/')],source='screenshot',status='observed',complete=False)
    assert (await send(client,app,sid))['status']=='completed'
    assert not scans
    assert soft_requests==['/','/flag.txt'] and flag_requests==['/','/flag.txt']
    assert all(row['tool_name']=='web.inspect' for row in app.state.db.rows('SELECT tool_name FROM tool_events'))


@pytest.mark.parametrize('instruction',[
    'Explain what flag.txt means',
    'Read the screenshot containing flag.txt',
    'Find flag.txt using my configured SSH profile',
])
def test_goal_routing_does_not_override_explanations_images_or_explicit_ssh(instruction):
    from blackbox.core.goals import goal_request
    assert not goal_request(instruction)


async def test_open_unknown_service_is_verified_by_head_before_http_goal(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    async def unknown(ctx,args):
        return ToolResult(ok=True,exit_code=0,summary='Open port; service unknown',evidence=[
            Finding(kind='port',target='127.0.0.1',port=port,protocol='tcp',state='open',service='unknown').model_dump()])
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',unknown)
    assert (await send(client,app,sid))['status']=='completed'
    rows=app.state.db.rows('SELECT * FROM tool_events ORDER BY started_at')
    assert [r['tool_name'] for r in rows]==['net.scan_ports','web.inspect','web.inspect','web.inspect']
    assert json.loads(rows[1]['args_json'])['method']=='HEAD'
    assert json.loads(rows[1]['output_json'])['evidence'][0]['status']==200
    assert requests==['/','/','/flag.txt']
    assert GoalController.load(app.state.db,sid).result['content']=='THM{independent_local_evidence}'
    assert any('Protocol hypothesis only' in e.get('action',{}).get('rationale','') for e in app.state.db.events(sid))


async def test_failed_unknown_protocol_probe_does_not_fabricate_http_or_access(client,app,flag_server,monkeypatch):
    port,requests=flag_server
    sid,_=await setup_goal(client,app,port,monkeypatch)
    async def unknown(ctx,args):
        return ToolResult(ok=True,summary='Unknown listener',evidence=[Finding(kind='port',target='127.0.0.1',port=port,protocol='tcp',state='open',service='unknown').model_dump()])
    async def not_http(ctx,args):
        assert args.method=='HEAD'
        return ToolResult(ok=False,summary='Peer did not speak HTTP')
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',unknown)
    monkeypatch.setattr(app.state.agent.registry.specs['web.inspect'],'run',not_http)
    assert (await send(client,app,sid))['status']=='needs_access'
    assert not GoalController.load(app.state.db,sid).result and not requests
    names=[r['tool_name'] for r in app.state.db.rows('SELECT tool_name FROM tool_events')]
    assert names==['net.scan_ports','web.inspect']
