"""Argument/recovery regressions use isolated workspace and mocked network only."""
import json
import os

import httpx
import pytest

from blackbox.tools.base import Arguments,ToolResult
from blackbox.tools.validation import EXAMPLES
from blackbox.tools import workspace
from conftest import call
from test_api import new,finish
from test_tools import context
from scripts.qa_support import agent_findings


async def test_all_registered_tools_have_strict_validated_examples(app):
    registry=app.state.agent.registry
    assert set(EXAMPLES)==set(registry.specs)
    for spec in registry.specs.values():
        assert issubclass(spec.argument_schema,Arguments)
        schema=spec.schema()['function']['parameters']
        assert schema['additionalProperties'] is False
        assert schema['examples']
        for example in schema['examples']:
            spec.argument_schema.model_validate(example)
    read=registry.specs['workspace.read'].schema()['function']['parameters']
    assert 'path' in read['required'] and 'relative' in read['properties']['path']['description']


async def test_safe_workspace_path_and_integer_repairs(context):
    root=context.settings.data_dir/'workspace'
    (root/'notes.txt').write_text('actual workspace evidence')
    registry=context.agent.registry
    for path in ['./notes.txt',str(root/'notes.txt')]:
        result,tid=await registry.execute(context,'workspace.read',{'path':path,'max_bytes':'8192'})
        assert result.ok and result.stdout=='actual workspace evidence'
        assert result.validated_arguments=={'path':'notes.txt','max_bytes':8192}
    assert any(e['type']=='tool_arguments_normalized' for e in context.db.events(context.sid))


@pytest.mark.parametrize('path',['/home/lab/flag.txt','../notes.txt','docs/../notes.txt','file:///etc/hosts','~/notes.txt'])
async def test_ambiguous_or_escaping_paths_are_never_guessed(context,path,monkeypatch):
    ran=[]
    async def forbidden(ctx,args):
        ran.append(args)
        return ToolResult(ok=True,stdout='fabricated')
    monkeypatch.setattr(context.agent.registry.specs['workspace.read'],'run',forbidden)
    result,_=await context.agent.registry.execute(context,'workspace.read',{'path':path})
    assert not result.ok and result.error.kind=='validation' and not ran
    assert result.error.suggestions


async def test_symlink_escape_is_denied_without_opening_secret(context,tmp_path):
    outside=tmp_path/'outside.txt';outside.write_text('PRIVATE OUTSIDE')
    root=context.settings.data_dir/'workspace'
    (root/'link.txt').symlink_to(outside)
    result,_=await context.agent.registry.execute(context,'workspace.read',{'path':'link.txt'})
    assert not result.ok and result.error.kind=='policy_denial'
    assert 'PRIVATE OUTSIDE' not in result.model_dump_json()


async def test_symlink_swap_between_validation_and_open_is_confined(context,tmp_path,monkeypatch):
    root=context.settings.data_dir/'workspace'
    (root/'reports').mkdir();(root/'reports/note.txt').write_text('inside')
    outside=tmp_path/'external';outside.mkdir();(outside/'note.txt').write_text('OUTSIDE SECRET')
    original=os.open
    swapped=[]
    def open_race(path,flags,*args,**kwargs):
        if path=='reports' and kwargs.get('dir_fd') is not None and not swapped:
            swapped.append(True)
            (root/'reports').rename(root/'saved-reports')
            (root/'reports').symlink_to(outside,target_is_directory=True)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',open_race)
    result,_=await context.agent.registry.execute(context,'workspace.read',{'path':'reports/note.txt'})
    assert not result.ok and swapped
    assert 'OUTSIDE SECRET' not in result.model_dump_json()


async def test_repeated_bad_workspace_call_recovers_by_listing(client,app,mock_ollama,monkeypatch):
    sid=await new(client)
    (app.state.settings.data_dir/'workspace/notes.txt').write_text('REAL LOCAL CONTENT')
    attempts=[]
    original=app.state.agent.registry.specs['workspace.read'].run
    async def read(ctx,args):
        attempts.append(args.path)
        return await original(ctx,args)
    monkeypatch.setattr(app.state.agent.registry.specs['workspace.read'],'run',read)
    mock_ollama.replies=[call('workspace.read',path='/home/lab/flag.txt'),
                         call('workspace.read',path='/home/lab/flag.txt'),
                         call('workspace.list',path='.'),call('workspace.read',path='notes.txt'),
                         {'content':'Read the actual workspace notes. No remote file access was established.'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read the notes from the workspace and explain what you found'})
    assert (await finish(app,sid))['status']=='completed'
    assert attempts==['notes.txt'],app.state.db.rows('SELECT tool_name,args_json,output_json FROM tool_events')
    events=app.state.db.events(sid)
    assert any(e['type']=='tool_retry_suppressed' for e in events)
    observations=[json.loads(m['content']) for m in app.state.db.messages(sid) if m['role']=='tool']
    assert observations[0]['error']['kind']=='validation'
    assert observations[1]['error']['code']=='IDENTICAL_FAILURE_SUPPRESSED'
    assert any(o.get('stdout')=='REAL LOCAL CONTENT' for o in observations)
    assert 'No remote file access' in app.state.db.messages(sid)[-1]['content']


async def test_local_workspace_workflow_recovers_and_reports_verified_content(
        client, app, mock_ollama, monkeypatch, record_property):
    sid = await new(client)
    note = 'Maintenance window: Thursday at 18:00.'
    (app.state.settings.data_dir/'workspace/maintenance-backup.txt').write_text(note)
    reads = []
    spec = app.state.agent.registry.specs['workspace.read']
    original = spec.run

    async def read(ctx, args):
        reads.append(args.path)
        if args.path == 'maintenance.txt':
            raise TimeoutError('Controlled workspace read timeout')
        result = await original(ctx, args)
        assert result.ok
        mock_ollama.replies.append({'content': result.stdout})
        return result

    monkeypatch.setattr(spec, 'run', read)
    mock_ollama.replies = [
        call('workspace.read', path='../invalid.txt'),
        call('workspace.read', path='../invalid.txt'),
        call('workspace.list', path='.'),
        call('workspace.read', path='maintenance.txt'),
        call('workspace.read', path='maintenance-backup.txt'),
    ]
    response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Read workspace file maintenance-backup.txt'})
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == 'completed'
    messages = app.state.db.messages(sid)
    observations = [json.loads(message['content']) for message in messages if message['role'] == 'tool']
    assert len(observations) == 5
    assert [o['error']['kind'] for o in observations if not o['ok']] == ['validation', 'validation', 'transient']
    assert observations[1]['error']['code'] == 'IDENTICAL_FAILURE_SUPPRESSED'
    assert reads == ['maintenance.txt', 'maintenance-backup.txt']
    final = messages[-1]
    assert final['metadata']['conversation_kind'] == 'final'
    assert observations[2]['evidence'][0]['path'] == 'maintenance-backup.txt'
    assert observations[-1]['stdout'] == note
    assert final['content'].splitlines()[0] == note
    assert observations[-1]['tool_id'] in final['content']
    provider_tool_message = mock_ollama.requests[-1]['messages'][-1]
    assert provider_tool_message['role'] == 'tool'
    assert json.loads(provider_tool_message['content'])['stdout'] == note

    db = app.state.db
    assert not db.session(sid)['scope']
    events = db.events(sid)
    tools = db.rows('SELECT * FROM tool_events ORDER BY started_at')
    assert {tool['tool_name'] for tool in tools} == {'workspace.read', 'workspace.list'}
    assert len(tools) == len(observations)
    assert events[-1]['type'] == 'run_finished' and events[-1]['reason'] == 'completed'
    assert not agent_findings(events, tools, required_tools=('workspace.read', 'workspace.list'),
                              expected_text=note, final_text=final['content'], terminal='completed')

    executions = [(e['name'], e['args']['path']) for e in events if e['type'] == 'tool_started']
    assert executions == [('workspace.list', '.'), ('workspace.read', 'maintenance.txt'),
                          ('workspace.read', 'maintenance-backup.txt')]
    record_property('objective_completion_rate', '1/1 (100%)')
    record_property('tool_outcomes', len(observations))
    record_property('successful_executions', sum(tool['status'] == 'success' for tool in tools))
    record_property('executions', len(executions))
    record_property('redundant_executions', len(executions) - len(set(executions)))
    record_property('duplicate_proposals_suppressed', sum(o['error']['code'] == 'IDENTICAL_FAILURE_SUPPRESSED'
                                                          for o in observations if not o['ok']))
    record_property('recovered_validation_errors', sum(o['error']['kind'] == 'validation'
                                                      and o['error']['code'] != 'IDENTICAL_FAILURE_SUPPRESSED'
                                                      for o in observations if not o['ok']))
    record_property('recovered_transient_failures', sum(o['error']['kind'] == 'transient'
                                                       for o in observations if not o['ok']))


async def test_repeated_http_connection_failure_is_not_reexecuted(client,app,mock_ollama,monkeypatch):
    sid=await new(client)
    assert (await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':'8080,8081'})).status_code==200
    attempts=[]
    async def inspect(ctx,args):
        attempts.append(args.url)
        if ':8080' in args.url:
            raise httpx.ConnectError('Controlled connection refused')
        return ToolResult(ok=True,stdout='Controlled HTTP service',summary='HTTP 200',evidence=[{'url':args.url,'status':200}])
    monkeypatch.setattr(app.state.agent.registry.specs['web.inspect'],'run',inspect)
    mock_ollama.replies=[call('web.inspect',url='http://127.0.0.1:8080/'),call('web.inspect',url='http://127.0.0.1:8080/'),
                         call('web.inspect',url='http://127.0.0.1:8081/'),{'content':'Port 8080 was unreachable; the supplied alternative 8081 returned HTTP 200. No vulnerability was verified.'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Inspect the provided local endpoints on ports 8080 and 8081, recovering if one fails'})
    assert (await finish(app,sid))['status']=='completed'
    assert attempts==['http://127.0.0.1:8080/','http://127.0.0.1:8081/']
    errors=[json.loads(m['content'])['error'] for m in app.state.db.messages(sid) if m['role']=='tool']
    assert errors[0]['kind']=='transient' and errors[1]['code']=='IDENTICAL_FAILURE_SUPPRESSED'


async def test_missing_tool_and_unknown_fields_are_actionable(context,monkeypatch):
    registry=context.agent.registry
    monkeypatch.setitem(registry.runner.binaries,'nmap',None)
    missing,_=await registry.execute(context,'net.scan_ports',{'target':'127.0.0.1','ports':'80'})
    assert missing.error.kind=='unavailable'
    invalid,_=await registry.execute(context,'workspace.read',{'path':'notes.txt','command':'cat /etc/shadow'})
    assert invalid.error.kind=='validation'
    assert invalid.error.fields[0]['field']=='command'
    assert invalid.validated_arguments is None
    assert all('input' not in field for field in invalid.error.fields)


async def test_missing_file_is_permanent_but_bad_types_are_validation(context):
    missing,_=await context.agent.registry.execute(context,'workspace.read',{'path':'absent.txt'})
    assert missing.error.kind=='permanent' and missing.error.code=='WORKSPACE_FILE_UNAVAILABLE'
    invalid,_=await context.agent.registry.execute(context,'workspace.read',{'path':['notes.txt']})
    assert invalid.error.kind=='validation' and invalid.validated_arguments is None


@pytest.mark.parametrize('raw',[None,[],'not an argument object'])
async def test_nonobject_arguments_are_structured_errors(context,raw):
    result,_=await context.agent.registry.execute(context,'workspace.read',raw)
    assert not result.ok and result.error.kind=='validation'
    assert result.validated_arguments is None


async def test_no_unbounded_recovery_when_model_ignores_errors(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('workspace.read',path='../bad')]*8
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read the workspace notes'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==2
    assert 'identical action' in app.state.db.messages(sid)[-1]['content']
