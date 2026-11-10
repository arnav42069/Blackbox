"""Dependency matrices and fallback behavior use only isolated local fixtures."""
import pytest

from blackbox.dependencies import detect
from blackbox.core.planner import Planner
from test_tools import context, web_server, execute
from test_api import new, finish


def test_missing_dependency_inventory(monkeypatch):
    monkeypatch.setattr('blackbox.dependencies.shutil.which',lambda name:None)
    report=detect()
    assert not report['baseline_ready']
    assert {name for name,p in report['packages'].items() if p['tier']=='baseline'}=={'iputils','nmap','openssh'}
    assert report['packages']['ffuf']['tier']=='preferred'
    assert report['packages']['whatweb']['tier']=='optional'
    assert report['capabilities']['http_discovery']=='native_http'
    assert report['capabilities']['screenshot_upload'] and report['capabilities']['browser_image_paste']
    assert 'wl-copy' in report['binaries'] and 'wl-paste' in report['binaries']
    assert all('Install:' in message for message in report['messages'])


def test_baseline_present_optional_absent(monkeypatch):
    monkeypatch.setattr('blackbox.dependencies.shutil.which',lambda name:f'/usr/bin/{name}' if name in {'ping','nmap','ssh'} else None)
    report=detect()
    assert report['baseline_ready']
    assert not report['packages']['grim']['available']
    assert report['capabilities']['http_discovery']=='native_http'


async def test_ffuf_preferred_and_native_fallback(context,web_server,monkeypatch):
    port,requests=web_server
    url=f'http://127.0.0.1:{port}/'
    monkeypatch.setitem(context.runner.binaries,'ffuf','/usr/bin/ffuf')
    plan=await Planner().build(context,'Fuzz '+url)
    assert plan.tool=='web.discover_paths' and 'ffuf' in plan.rationale
    monkeypatch.setitem(context.runner.binaries,'ffuf',None)
    context.settings.prefs.http_rate=5
    plan=await Planner().build(context,'Fuzz '+url)
    assert plan.state=='ready' and 'native HTTP discovery' in plan.rationale
    assert any(s['function']['name']=='web.discover_paths' for s in context.agent.registry.schemas('http_discovery'))
    async def forbidden(*args,**kwargs):
        raise AssertionError('native fallback must not spawn SSH or another subprocess')
    monkeypatch.setattr(context.runner,'run',forbidden)
    result=await execute(context,'web.discover_paths',url=url,rate=5)
    assert result.ok and 'fallback' in result.summary
    assert len(requests)==12 and context.requests==12
    assert all(f['status']==200 for f in result.evidence)
    # Explicitly bypass the saved result to test fallback budget enforcement.
    context.scope.data['max_requests_per_run']=context.requests
    result=await execute(context,'web.discover_paths',url=url,rate=5,rescan=True)
    assert result.denied_reason and len(requests)==12


async def test_fallback_rejects_out_of_scope_and_timeout(context,web_server,monkeypatch):
    monkeypatch.setitem(context.runner.binaries,'ffuf',None)
    port,requests=web_server
    result=await execute(context,'web.discover_paths',url='http://192.0.2.1/')
    assert result.denied_reason and not requests
    context.settings.prefs.discovery_timeout_seconds=1
    result=await execute(context,'web.discover_paths',url=f'http://127.0.0.1:{port}/',timeout_seconds=1)
    assert not result.ok and len(requests)<12


async def test_browser_upload_without_native_capture_tools(client,app,png,monkeypatch):
    monkeypatch.setattr('blackbox.dependencies.shutil.which',lambda name:None)
    app.state.dependencies.update(detect())
    response=await client.get('/api/preflight')
    assert response.json()['dependencies']['capabilities']['screenshot_upload']
    assert not response.json()['dependencies']['baseline_ready']
    sid=await new(client)
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read screenshot'},files={'images':('screen.png',png,'image/png')})
    assert response.status_code==200
    assert (await finish(app,sid))['status']=='completed'


async def test_missing_whatweb_falls_back_to_limited_headers(context,web_server,monkeypatch):
    port,_=web_server
    monkeypatch.setitem(context.runner.binaries,'whatweb',None)
    plan=await Planner().build(context,f'Identify web technologies at http://127.0.0.1:{port}/')
    assert plan.tool=='web.inspect' and plan.arguments['method']=='HEAD'
    assert 'No technology fingerprint is claimed' in plan.rationale
    result=await execute(context,plan.tool,**plan.arguments)
    assert result.ok and result.evidence[0]['status']==200
