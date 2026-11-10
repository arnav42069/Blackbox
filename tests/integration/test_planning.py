"""No public targets: real ffuf/HTTP fixture, mocked Ollama and Nmap outputs."""
import asyncio
import io
import json
import time

import pytest
from PIL import Image, ImageDraw

from blackbox.core.planner import Planner
from blackbox.core.scans import ScanStore, Finding
from blackbox.storage.db import now, uid
from blackbox.tools.base import ToolResult
from test_api import new, finish
from test_tools import context, web_server, execute


def nmap_xml(port):
    return f'<nmaprun><host><status state="up"/><address addr="127.0.0.1"/><ports><port portid="{port}" protocol="tcp"><state state="open"/><service name="http"/></port></ports></host><runstats><finished exit="success"/></runstats></nmaprun>'


def fake_nmap(context, monkeypatch):
    calls = []
    async def run(binary, args, **kwargs):
        assert binary=='nmap'
        calls.append(args)
        from pathlib import Path
        Path(args[args.index('-oX')+1]).write_text(nmap_xml(8080))
        return ToolResult(ok=True, exit_code=0, stdout='8080/tcp open http')
    monkeypatch.setattr(context.runner, 'run', run)
    return calls


async def test_fresh_results_equivalent_arguments_and_explicit_rescan(context, monkeypatch):
    calls = fake_nmap(context, monkeypatch)
    first = await execute(context,'net.scan_ports', target='127.0.0.1', ports='8080')
    second = await execute(context,'net.scan_ports', target='127.0.0.1', ports='8080-8080')
    assert first.ok and second.ok and second.cached_scan_id and len(calls)==1
    third = await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080',rescan=True)
    assert third.ok and not third.cached_scan_id and len(calls)==2
    assert len(context.db.rows('SELECT * FROM scan_results'))==2
    assert any(e['type']=='scan_reused' for e in context.db.events(context.sid))


async def test_stale_failed_and_incomplete_scans_are_never_cache_hits(context, monkeypatch):
    calls = fake_nmap(context, monkeypatch)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    context.db.execute('UPDATE scan_results SET observed_at=?',(now()-10000,))
    plan = await Planner().build(context,'Fuzz the web server')
    assert plan.state=='needs_target' and plan.previous_findings[0]['freshness']=='stale'
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    assert len(calls)==2
    for status, complete in [('failed',1),('success',0)]:
        context.db.execute('UPDATE scan_results SET status=?,complete=?,observed_at=?',(status,complete,now()))
        plan = await Planner().build(context,'Fuzz the web server')
        assert plan.state=='needs_target'
        result = await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
        assert not result.cached_scan_id
    assert len(calls)==4


async def test_scope_and_session_changes_invalidate_cache(context, monkeypatch):
    calls = fake_nmap(context, monkeypatch)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    context.scope.data['name']='new approval'
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    assert len(calls)==2
    context.scope.data['allowed_targets']=['192.0.2.1']
    denied = await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    assert denied.denied_reason and len(calls)==2
    assert not await ScanStore(context.db).relevant(context)


async def test_planner_reuses_nonstandard_http_and_limits(context, monkeypatch):
    fake_nmap(context,monkeypatch)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    plan = await Planner().build(context,'Fuzz the web server')
    assert plan.tool=='web.discover_paths' and plan.state=='ready'
    assert plan.arguments['url']=='http://127.0.0.1:8080/'
    assert plan.arguments['rate']==2 and plan.constraints.request_budget==100
    assert 'SSH' in plan.rationale
    assert not any(s['function']['name'].startswith('ssh.') for s in context.agent.registry.schemas(plan.intent))


async def test_ambiguous_endpoints_require_exact_url(context):
    store = ScanStore(context.db)
    for port in [8080,8081]:
        store.save(context.sid,context.scope.data,'vision.observe',{},[Finding(kind='port',target='127.0.0.1',port=port,state='open',url=f'http://127.0.0.1:{port}/')], source='screenshot', status='observed', complete=False)
    plan=await Planner().build(context,'Fuzz the web server')
    assert plan.state=='needs_target' and 'Multiple HTTP endpoints' in plan.rationale


async def test_out_of_scope_fuzz_rejected_before_subprocess(context, monkeypatch):
    calls=[]
    async def run(*args,**kwargs):
        calls.append(args)
        raise AssertionError('must not execute')
    monkeypatch.setattr(context.runner,'run',run)
    result=await execute(context,'web.discover_paths',url='http://192.0.2.1:8080/')
    assert result.denied_reason and not calls


async def test_http_budget_configured_rate_timeout_and_failure(context, web_server, monkeypatch):
    port,requests=web_server
    url=f'http://127.0.0.1:{port}/'
    for kwargs in [{'rate':3},{'concurrency':2},{'timeout_seconds':41}]:
        # concurrency defaults to one in configuration.
        result=await execute(context,'web.discover_paths',url=url,**kwargs)
        assert not result.ok
    assert not requests
    context.scope.data['max_requests_per_run']=11
    result=await execute(context,'web.discover_paths',url=url)
    assert result.denied_reason and not requests
    context.scope.data['max_requests_per_run']=100
    context.settings.prefs.discovery_timeout_seconds=1
    started=time.monotonic()
    result=await execute(context,'web.discover_paths',url=url,timeout_seconds=1)
    assert not result.ok and len(requests)<12 and time.monotonic()-started<5
    assert all(not r['complete'] for r in context.db.rows("SELECT complete FROM scan_results WHERE status='failed'"))


async def test_http_executor_failure_has_no_fabricated_results(context,web_server,monkeypatch):
    # A fresh run tests execution failure; replaying the timed-out call in the
    # previous test is deliberately suppressed by per-run failure recovery.
    port,_=web_server
    url=f'http://127.0.0.1:{port}/'
    async def fail(*args,**kwargs):
        return ToolResult(ok=False,exit_code=2,stderr='fixture failure',summary='ffuf exited 2')
    monkeypatch.setattr(context.runner,'run',fail)
    result=await execute(context,'web.discover_paths',url=url,timeout_seconds=1)
    assert not result.ok and not result.evidence and '2' in result.summary


async def test_real_ffuf_rate_and_cache(context, web_server):
    port,requests=web_server
    context.settings.prefs.http_rate=5
    started=time.monotonic()
    result=await execute(context,'web.discover_paths',url=f'http://127.0.0.1:{port}/',rate=5)
    elapsed=time.monotonic()-started
    assert result.ok and len(requests)==12 and context.requests==12
    assert 1.8 <= elapsed < 10
    assert all(e['status']==200 for e in result.evidence)
    cached=await execute(context,'web.discover_paths',url=f'http://127.0.0.1:{port}',rate=5)
    assert cached.cached_scan_id and len(requests)==12 and context.requests==12


async def test_internal_import_and_lookup(context):
    path=context.settings.data_dir/'workspace'/'scan.xml'
    path.write_text(nmap_xml(8080))
    result=await execute(context,'scan.parse',path='scan.xml',format='nmap_xml',observed_at=now()-10)
    assert result.ok
    lookup=await execute(context,'scan.lookup',target='127.0.0.1')
    assert lookup.ok and lookup.evidence[0]['source']=='import' and lookup.evidence[0]['freshness']=='fresh'
    plan=await Planner().build(context,'Fuzz the web server')
    assert plan.tool=='web.discover_paths' and plan.arguments['url']=='http://127.0.0.1:8080/'
    assert not await ScanStore(context.db).cached(context,'net.scan_ports',{'target':'127.0.0.1','ports':'8080','service_detection':False})


async def test_missing_ffuf_is_actionable(context,monkeypatch):
    monkeypatch.setitem(context.runner.binaries,'ffuf',None)
    plan=await Planner().build(context,'Fuzz http://127.0.0.1:8080/')
    assert plan.state=='ready' and 'native HTTP discovery' in plan.rationale


async def test_ip_alone_and_http_intent_cannot_trigger_ssh(client,app,mock_ollama):
    from conftest import call
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1']})
    mock_ollama.replies=[call('ssh.probe',target='127.0.0.1',profile='whatever')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'127.0.0.1'})
    assert (await finish(app,sid))['status']=='denied'
    assert not any(s['function']['name'].startswith('ssh.') for s in mock_ollama.requests[0]['tools'])
    assert 'IP address' in app.state.db.messages(sid)[-1]['content']


async def test_text_model_fallback_vision_never_sends_images_to_text_model(client,app,mock_ollama,png,monkeypatch):
    original=app.state.ollama.capabilities
    async def caps(model):
        if model=='vision:fixture':
            return {'vision':True,'tools':False}
        return {**(await original(model)), 'vision':False}
    monkeypatch.setattr(app.state.ollama,'capabilities',caps)
    sid=await new(client)
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read screenshot'},files={'images':('screen.png',png,'image/png')})
    assert response.status_code==400 and not mock_ollama.vision_requests
    app.state.settings.prefs.vision_model='vision:fixture'
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read screenshot'},files={'images':('screen.png',png,'image/png')})
    assert response.status_code==200
    assert (await finish(app,sid))['status']=='completed'
    assert mock_ollama.vision_requests[0]['model']=='vision:fixture'
    assert not mock_ollama.requests


async def test_invalid_vision_output_never_becomes_scan_findings(client,app,mock_ollama,png):
    mock_ollama.image_observation={'category':'scan','visible_text':'8080/tcp open http','uncertainties':[], 'invented_hosts':['127.0.0.1']}
    sid=await new(client)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read screenshot'},files={'images':('screen.png',png,'image/png')})
    assert (await finish(app,sid))['status']=='failed'
    assert not app.state.db.rows('SELECT * FROM scan_results')


async def test_screenshot_to_real_ffuf_workflow(client,app,mock_ollama,web_server,tmp_path):
    port,requests=web_server
    transcript=f'Nmap scan report for 127.0.0.1\nPORT      STATE SERVICE\n{port}/tcp open  http\n22/tcp closed ssh'
    image=Image.new('RGB',(1000,220),'black')
    ImageDraw.Draw(image).multiline_text((20,20),transcript,fill='white',font_size=24)
    output=io.BytesIO()
    image.save(output,format='PNG')
    mock_ollama.image_observation={'category':'scan','visible_text':transcript,'uncertainties':['Screenshot scan time is not visible.']}
    sid=await new(client)
    scope={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'1-65535','allow_active_scanning':True,'max_requests_per_run':24}
    assert (await client.put(f'/api/sessions/{sid}/scope',json=scope)).status_code==200
    app.state.settings.prefs.http_rate=5
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read this screenshot'},files={'images':('nmap.png',output.getvalue(),'image/png')})
    assert response.status_code==200
    assert (await finish(app,sid))['status']=='completed'
    assert not requests
    records=app.state.db.rows('SELECT * FROM scan_results')
    assert len(records)==1 and records[0]['source']=='screenshot' and not records[0]['complete']
    findings=json.loads(records[0]['findings_json'])
    assert {f['port'] for f in findings if f['kind']=='port'}=={22,port}
    assert not any(f['port']==443 for f in findings)
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Fuzz the web server.'})
    assert (await finish(app,sid))['status']=='completed', app.state.db.events(sid)
    tools=app.state.db.rows('SELECT * FROM tool_events')
    assert [t['tool_name'] for t in tools]==['web.discover_paths']
    argv=next(e['argv'] for e in app.state.db.events(sid) if e['type']=='tool_command')
    assert argv[0].endswith('/ffuf') and argv[argv.index('-rate')+1]=='5' and '-maxtime' in argv
    assert len(requests)==12
    assert len(app.state.db.rows("SELECT * FROM scan_results WHERE source='execution' AND status='success'"))==1
    answer=app.state.db.messages(sid)[-1]['content']
    assert '200' in answer and 'Evidence:' in answer and 'not a verified live scan' in answer
    # Repeat discovery reuses actual HTTP findings, without rereading the image or rescanning.
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Fuzz the web server.'})
    assert (await finish(app,sid))['status']=='completed' and len(requests)==12
    assert len(mock_ollama.vision_requests)==1 and not mock_ollama.requests


async def test_http_inspection_seeds_discovery_and_curl_headers(context,web_server):
    port,_=web_server
    response=await execute(context,'web.inspect',url=f'http://127.0.0.1:{port}/',method='HEAD')
    assert response.ok and response.evidence[0]['status']==200
    assert any(e['argv'][0].endswith('/curl') for e in context.db.events(context.sid) if e['type']=='tool_command')
    plan=await Planner().build(context,'Fuzz the web server')
    assert plan.state=='ready' and plan.arguments['url']==f'http://127.0.0.1:{port}/'


async def test_most_recent_conversation_target_is_respected(context):
    store=ScanStore(context.db)
    for target in ['127.0.0.1','127.0.0.2']:
        store.save(context.sid,context.scope.data,'vision.observe',{},[Finding(kind='port',target=target,port=8080,state='open',url=f'http://{target}:8080/')], source='screenshot',status='observed',complete=False)
    # Explicitly approve both loopback addresses, then establish the conversation target.
    context.scope.data['allowed_targets']=['127.0.0.0/8']
    context.db.message(context.sid,'user','Consider 127.0.0.2')
    context.db.message(context.sid,'user','Fuzz the web server')
    plan=await Planner().build(context,'Fuzz the web server')
    assert plan.arguments['url']=='http://127.0.0.2:8080/'


async def test_policy_blocks_ssh_for_http_intent(context):
    context.intent='http_discovery'
    result=await execute(context,'ssh.probe',target='127.0.0.1',profile='absent')
    assert result.denied_reason and 'SSH is unnecessary' in result.summary


async def test_tool_selection_for_tls_technology_and_sql_policy(context,monkeypatch):
    monkeypatch.setitem(context.runner.binaries,'whatweb','/usr/bin/whatweb')
    for instruction,tool in [('Inspect headers at http://127.0.0.1:8080/','web.inspect'),('Identify web technologies at http://127.0.0.1:8080/','web.technologies'),('Inspect TLS configuration at https://127.0.0.1:8443/','web.tls')]:
        plan=await Planner().build(context,instruction)
        assert plan.state=='ready' and plan.tool==tool
    sql=await Planner().build(context,'Test SQL injection in my authorized application')
    assert sql.state=='unsupported' and 'PRD' in sql.rationale


async def test_failed_deterministic_tool_is_reported_without_model_success(client,app,mock_ollama,monkeypatch):
    async def fail(*args,**kwargs):
        return ToolResult(ok=False,exit_code=2,summary='fixture tool failed')
    monkeypatch.setattr(app.state.agent.registry.runner,'run',fail)
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'8080','allow_active_scanning':True})
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Fuzz http://127.0.0.1:8080/'})
    assert (await finish(app,sid))['status']=='failed'
    assert 'No successful scan is claimed' in app.state.db.messages(sid)[-1]['content']
    assert not mock_ollama.requests


async def test_newer_failed_attempt_invalidates_older_success(context,monkeypatch):
    calls=fake_nmap(context,monkeypatch)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    async def failed(*args,**kwargs):
        return ToolResult(ok=False,exit_code=2,summary='Nmap fixture failure')
    monkeypatch.setattr(context.runner,'run',failed)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080',rescan=True)
    result=await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    assert not result.ok and not result.cached_scan_id
    assert (await Planner().build(context,'Fuzz the web server')).state=='needs_target'


async def test_cache_never_crosses_sessions(context,monkeypatch):
    from dataclasses import replace
    calls=fake_nmap(context,monkeypatch)
    await execute(context,'net.scan_ports',target='127.0.0.1',ports='8080')
    other_sid=context.db.create_session('fixture:4b')['id']
    rid=uid()
    context.db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,other_sid,now()))
    other=replace(context,sid=other_sid,rid=rid,calls=0)
    result=await execute(other,'net.scan_ports',target='127.0.0.1',ports='8080')
    assert result.ok and not result.cached_scan_id and len(calls)==2


async def test_existing_legacy_journal_is_parsed_without_claiming_scope_or_completeness(context):
    tid=uid()
    args={'target':'127.0.0.1','ports':'8080'}
    result=ToolResult(ok=True,stdout='Nmap scan report for 127.0.0.1\n8080/tcp open http',exit_code=0)
    context.db.execute('INSERT INTO tool_events VALUES (?,?,?,?,?,?,?,?,?)',(tid,context.rid,'net.scan_ports',json.dumps(args),result.model_dump_json(),0,'success',now()-30,now()-29))
    records=await ScanStore(context.db).relevant(context)
    assert len(records)==1 and records[0]['source']=='import' and not records[0]['complete']
    assert 'unknown' in records[0]['details']['verification']
    assert (await Planner().build(context,'Fuzz the web server')).arguments['url']=='http://127.0.0.1:8080/'
    assert not await ScanStore(context.db).cached(context,'net.scan_ports',args)
    assert len(context.db.rows('SELECT * FROM scan_results'))==1


async def test_screenshot_correlates_with_existing_scan_without_hiding_conflicts(client,app,mock_ollama,png):
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'8080'})
    scope=app.state.db.session(sid)['scope']
    ScanStore(app.state.db).save(sid,scope,'net.scan_ports',{'target':'127.0.0.1','ports':'8080'},[Finding(kind='port',target='127.0.0.1',port=8080,protocol='tcp',state='closed')],evidence_id='previous-scan',details={'result':ToolResult(ok=True).model_dump()})
    mock_ollama.image_observation={'category':'scan','visible_text':'Nmap scan report for 127.0.0.1\n8080/tcp open http','uncertainties':[]}
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read screenshot'},files={'images':('scan.png',png,'image/png')})
    assert (await finish(app,sid))['status']=='completed'
    observation=next(e['observations'] for e in app.state.db.events(sid) if e['type']=='image_observations')
    assert observation['correlations'][0]['relationship']=='conflicts'
    assert observation['correlations'][0]['evidence_id']=='previous-scan'
    assert observation['assumptions'] and 'inferred' in observation['assumptions'][0]


async def test_general_loop_cannot_claim_success_when_all_requested_tools_failed(client,app,mock_ollama,monkeypatch):
    from conftest import call
    async def fail(*args,**kwargs):
        return ToolResult(ok=False,summary='fixture failure',exit_code=2)
    monkeypatch.setattr(app.state.agent.registry.runner,'run',fail)
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'8080'})
    mock_ollama.replies=[call('web.inspect',url='http://127.0.0.1:8080/',method='HEAD'),{'content':'All succeeded!'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Inspect target'})
    assert (await finish(app,sid))['status']=='failed'
    assert 'No successful scan or access is claimed' in app.state.db.messages(sid)[-1]['content']
    assert 'All succeeded' not in app.state.db.messages(sid)[-1]['content']


async def test_positive_or_guessed_probe_cannot_promote_text_only_metadata(app,mock_ollama):
    mock_ollama.vision=False
    # This fixture would guess "red square" if sent a shape probe, despite no vision.
    capability=await app.state.ollama.capabilities('fixture:4b')
    assert not capability['vision']
    probes=await app.state.ollama.probe('fixture:4b')
    assert not probes['vision']
    assert not any(m.get('images') for request in mock_ollama.requests for m in request['messages'])
    app.state.db.execute("UPDATE model_capabilities SET probe_json=?",(json.dumps({'vision':True}),))
    app.state.ollama.cache=None
    assert not (await app.state.ollama.capabilities('fixture:4b'))['vision']


async def test_rejected_arguments_do_not_leak_credentials_into_scan_storage(context):
    result=await execute(context,'web.discover_paths',url='http://user:fixture-private-value@127.0.0.1:8080/')
    assert not result.ok
    assert 'fixture-private-value' not in json.dumps(context.db.rows('SELECT * FROM scan_results'))


async def test_invalid_scan_target_cannot_poison_later_context_retrieval(context):
    result=await execute(context,'net.scan_ports',target=None)
    assert not result.ok
    result=await execute(context,'scan.lookup')
    assert result.ok and result.evidence==[]


async def test_natural_language_export_analysis_routes_to_parser_with_unknown_time_stale(client,app):
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'8080'})
    (app.state.settings.data_dir/'workspace'/'scan.xml').write_text(nmap_xml(8080))
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Analyze the existing Nmap scan.xml export'})
    assert (await finish(app,sid))['status']=='completed'
    assert app.state.db.one('SELECT tool_name FROM tool_events')['tool_name']=='scan.parse'
    record=app.state.db.one('SELECT * FROM scan_results')
    assert record['observed_at']==0 and record['source']=='import'
    assert 'unknown' in json.loads(record['details_json'])['timestamp_basis']
