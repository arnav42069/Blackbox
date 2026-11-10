"""Deterministic room scenarios; private lab addresses are never contacted."""
import json
import pytest
from blackbox.core.goals import GoalController
from blackbox.core.scans import ScanStore,Finding
from blackbox.tools.base import ToolResult
from blackbox.tools import network
from test_api import new,finish


async def lab(client,app,monkeypatch,target='10.49.150.109',allowed='80,443,18080'):
    sid=await new(client)
    async def authorize(target):
        r=await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':[target],'allowed_ports':allowed,'allow_active_scanning':True})
        assert r.status_code==200,r.text
    await authorize(target)
    async def preflight(ctx,target):
        return ToolResult(ok=True,summary='Mock VPN route and reachability',evidence=[{'target':target,'interface':'tun-fixture','vpn_route_verified':True}])
    monkeypatch.setattr(network,'preflight',preflight)
    monkeypatch.setitem(app.state.agent.registry.runner.binaries,'nmap','/mock/nmap')
    monkeypatch.setitem(app.state.agent.registry.runner.binaries,'whatweb',None)
    monkeypatch.setitem(app.state.agent.registry.runner.binaries,'ffuf',None)
    scans=[]
    reads=[]
    async def scan(ctx,args):
        scans.append((args.target,args.ports))
        evidence=[]
        if '18080' in args.ports:
            evidence=[Finding(kind='port',target=args.target,port=18080,protocol='tcp',service='http',state='open',url=f'http://{args.target}:18080/').model_dump()]
        return ToolResult(ok=True,exit_code=0,summary='Mocked scan completed',evidence=evidence)
    async def inspect(ctx,args):
        ctx.request()
        reads.append(args.url)
        path=args.url.split(':18080',1)[-1]
        body='<a href="/docs/">Documentation</a>' if path=='/' else '<a href="flag.txt">Artifact</a>' if path=='/docs/' else 'THM{'+ctx.run_target+'}' if path=='/docs/flag.txt' else 'missing'
        return ToolResult(ok=True,stdout=body,summary='Mock HTTP response',evidence=[{'url':args.url,'status':404 if body=='missing' else 200}])
    monkeypatch.setattr(app.state.agent.registry.specs['net.scan_ports'],'run',scan)
    monkeypatch.setattr(app.state.agent.registry.specs['web.inspect'],'run',inspect)
    return sid,authorize,scans,reads


async def send(client,app,sid,text):
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':text})
    assert response.status_code==200,response.text
    return await finish(app,sid)


async def test_target_switch_never_reuses_previous_target(client,app,monkeypatch):
    sid,authorize,scans,reads=await lab(client,app,monkeypatch)
    assert (await send(client,app,sid,'Solve TryHackMe at 10.49.150.109 and retrieve flag.txt'))['status']=='completed'
    first=GoalController.load(app.state.db,sid)
    await authorize('10.49.170.55')
    invalid=GoalController.load(app.state.db,sid)
    assert invalid.result is None and not invalid.verified_findings and not invalid.hypotheses
    assert (await send(client,app,sid,'Solve TryHackMe at 10.49.170.55 and retrieve flag.txt'))['status']=='completed'
    goal=GoalController.load(app.state.db,sid)
    assert goal.id!=first.id and goal.result['content']=='THM{10.49.170.55}'
    assert all(f['target']=='10.49.170.55' and f['run_id']==goal.last_run_id for f in goal.verified_findings)
    assert {target for target,_ in scans}=={'10.49.150.109','10.49.170.55'}
    assert any(e['type']=='target_context_changed' for e in app.state.db.events(sid))
    finals=[m for m in app.state.db.messages(sid) if m['metadata'].get('conversation_kind')=='final']
    assert finals[0]['metadata']['target']=='10.49.150.109' and finals[-1]['metadata']['target']=='10.49.170.55'


async def test_nonstandard_port_and_multistep_artifact_verification(client,app,monkeypatch):
    sid,_,scans,reads=await lab(client,app,monkeypatch)
    assert (await send(client,app,sid,'Solve TryHackMe at 10.49.150.109 and retrieve flag.txt'))['status']=='completed'
    assert scans==[('10.49.150.109','80,443'),('10.49.150.109','18080')]
    assert reads==[f'http://10.49.150.109:18080/{path}' for path in ['', 'flag.txt','docs/','docs/flag.txt']]
    goal=GoalController.load(app.state.db,sid)
    attempts=list(goal.attempts.values())
    assert attempts[0]['ok'] and not attempts[0]['advances_investigation'] and not attempts[0]['objective_achieved']
    assert all(not row['objective_achieved'] for row in attempts[:-1])
    assert attempts[-1]['objective_achieved'] and goal.result['content']=='THM{10.49.150.109}'
    phases={e['phase'] for e in app.state.db.events(sid) if e['type']=='goal_state'}
    assert phases=={'PLAN','EXECUTE','OBSERVE','EVALUATE','REPLAN'}


async def test_budget_checkpoint_resumes_missing_ports_without_rescan(client,app,monkeypatch):
    sid,_,scans,reads=await lab(client,app,monkeypatch)
    app.state.settings.prefs.max_tool_calls=1
    assert (await send(client,app,sid,'Solve TryHackMe at 10.49.150.109 and retrieve flag.txt'))['status']=='checkpointed'
    saved=GoalController.load(app.state.db,sid)
    assert saved.pending.arguments['ports']=='18080' and saved.budget['tool_calls_remaining']==0
    app.state.settings.prefs.max_tool_calls=8
    assert (await send(client,app,sid,'Resume goal'))['status']=='completed'
    assert scans==[('10.49.150.109','80,443'),('10.49.150.109','18080')]


async def test_http_candidate_cannot_satisfy_home_directory_objective(client,app,monkeypatch):
    sid,_,scans,reads=await lab(client,app,monkeypatch)
    # Keep discovery deterministic and bounded after the public candidate is read.
    app.state.settings.prefs.max_tool_calls=6
    result=await send(client,app,sid,'Retrieve flag.txt from the authorized TryHackMe machine 10.49.150.109 user home directory')
    goal=GoalController.load(app.state.db,sid)
    assert result['status']!='completed' and goal.result is None
    assert any('home directory' in message for message in goal.limitations)


async def test_missing_vpn_route_blocks_scan_with_visible_checkpoint(client,app,monkeypatch):
    sid,_,scans,reads=await lab(client,app,monkeypatch)
    async def blocked(ctx,target):
        return ToolResult(ok=False,summary='Target preflight blocked: no verified VPN route to target.')
    monkeypatch.setattr(network,'preflight',blocked)
    result=await send(client,app,sid,'Solve TryHackMe at 10.49.150.109 and retrieve flag.txt')
    assert result['status']=='needs_access' and not scans and not reads
    assert 'VPN route' in app.state.db.messages(sid)[-1]['content']


async def test_actual_preflight_requires_vpn_but_not_icmp_success(client,app,monkeypatch):
    from blackbox.tools.registry import ToolContext
    from blackbox.core.scope import Scope
    actual_preflight=network.preflight
    sid,_,_,_=await lab(client,app,monkeypatch)
    ctx=ToolContext(app.state.settings,app.state.db,sid,'fixture-run',Scope(app.state.db.session(sid)['scope']),app.state.agent.registry.runner,app.state.agent,run_target='10.49.150.109',require_vpn=True)
    calls=[]
    interface='eth0'
    async def run(binary,args,**kwargs):
        calls.append((binary,args))
        return ToolResult(ok=True,stdout=json.dumps([{'dev':interface}])) if binary=='ip' else ToolResult(ok=False,summary='ICMP silent')
    monkeypatch.setitem(ctx.runner.binaries,'ip','/mock/ip')
    monkeypatch.setattr(ctx.runner,'run',run)
    blocked=await actual_preflight(ctx,ctx.run_target)
    assert not blocked.ok and [name for name,_ in calls]==['ip']
    interface='tun0'
    allowed=await actual_preflight(ctx,ctx.run_target)
    assert allowed.ok and allowed.evidence[0]['icmp_reachable'] is None
    assert [name for name,_ in calls]==['ip','ip','ping']


async def test_exact_run_target_is_stricter_than_multi_host_scope(client,app,monkeypatch):
    from blackbox.tools.registry import ToolContext
    from blackbox.core.scope import Scope
    sid,_,_,_=await lab(client,app,monkeypatch)
    scope=app.state.db.session(sid)['scope']
    scope['allowed_targets'].append('10.49.170.55')
    ctx=ToolContext(app.state.settings,app.state.db,sid,'fixture-run',Scope(scope),app.state.agent.registry.runner,app.state.agent,run_target='10.49.150.109')
    spec=app.state.agent.registry.specs['web.inspect']
    from blackbox.tools.web import Inspect
    from blackbox.core.scope import ScopeDenied
    with pytest.raises(ScopeDenied,match='exact target'):
        await app.state.agent.registry.policy.validate_destination(ctx,spec,Inspect(url='http://10.49.170.55:80/'))


async def test_large_authorized_port_range_advances_past_first_batch(client,app,monkeypatch):
    from blackbox.core.scope import ports
    sid,_,scans,_=await lab(client,app,monkeypatch,allowed='1-20000')
    app.state.settings.prefs.max_tool_calls=3
    assert (await send(client,app,sid,'Solve TryHackMe at 10.49.150.109 and retrieve flag.txt'))['status']=='checkpointed'
    assert len(scans)==3
    seen=set()
    for _,selected in scans:
        batch=ports(selected)
        assert len(batch)<=1000 and not batch & seen
        seen.update(batch)
    assert max(seen)>1000
    assert GoalController.load(app.state.db,sid).pending.tool=='net.scan_ports'


async def test_home_objective_requires_actual_matching_sftp_file_read(client,app,monkeypatch):
    sid,_,_,_=await lab(client,app,monkeypatch,allowed='22')
    monkeypatch.setattr('blackbox.tools.ssh.public_profiles',lambda settings:[{'name':'fixture','host':'10.49.150.109','port':22,'read_roots':['/home']}])
    async def find(ctx,args):
        return ToolResult(ok=True,summary='Found matching file',evidence=[{'path':'/home/lab/flag.txt'}])
    async def read(ctx,args):
        return ToolResult(ok=True,summary='Actual mocked SFTP read',stdout='THM{verified_home_file}',evidence=[{'path':args.path}])
    monkeypatch.setattr(app.state.agent.registry.specs['ssh.find_files'],'run',find)
    monkeypatch.setattr(app.state.agent.registry.specs['ssh.read_file'],'run',read)
    assert (await send(client,app,sid,'Retrieve flag.txt from the authorized TryHackMe machine 10.49.150.109 user home directory'))['status']=='completed'
    goal=GoalController.load(app.state.db,sid)
    assert goal.result['path']=='/home/lab/flag.txt' and goal.result['content']=='THM{verified_home_file}'
    attempts=list(goal.attempts.values())
    assert not attempts[-2]['objective_achieved'] and attempts[-1]['objective_achieved']
