"""Authorized loopback goal integration: real Nmap, ffuf and HTTP, no SSH keys.

Ollama metadata is mocked by conftest; goal planning does not call a model.
Never reads a remote lab target or expands beyond the ephemeral fixture port.
"""
import asyncio
import json
from pathlib import Path
import secrets
import shutil
import time

import pytest

from blackbox.core.goals import GoalController
from test_api import new, finish


async def test_real_discovery_goal_without_ssh_credentials(client,app,mock_ollama,monkeypatch):
    if not shutil.which('nmap') or not shutil.which('ffuf'):
        pytest.skip('Real goal integration requires installed nmap and preferred ffuf')
    monkeypatch.delenv('SSH_AUTH_SOCK',raising=False)
    assert not (app.state.settings.config_dir/'ssh_profiles.yaml').exists()
    planted='THM{'+secrets.token_hex(16)+'}'
    requests=[]
    async def handler(reader,writer):
        try:
            data=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
            parts=data.split(b'\r\n',1)[0].split(b' ')
            path=parts[1].decode(errors='replace') if len(parts)>=2 else ''
            requests.append({'path':path,'at':time.monotonic()})
            status='200 OK' if path in {'/','/docs','/docs/flag.txt'} else '404 Not Found'
            body=planted.encode() if path=='/docs/flag.txt' else b'<html><title>Controlled local lab</title></html>'
            writer.write(f'HTTP/1.1 {status}\r\nServer: BlackBox-Local-Fixture\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        except (asyncio.IncompleteReadError,TimeoutError,ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    try:
        port=server.sockets[0].getsockname()[1]
        sid=await new(client)
        scope={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(port),
               'allow_active_scanning':True,'max_requests_per_run':32}
        assert (await client.put(f'/api/sessions/{sid}/scope',json=scope)).status_code==200
        app.state.settings.prefs.http_rate=5
        response=await client.post(f'/api/sessions/{sid}/messages',data={
            'text':'Solve the authorized TryHackMe room at 127.0.0.1 and retrieve flag.txt'})
        assert response.status_code==200
        session=await finish(app,sid)
        events=app.state.db.events(sid)
        goal=GoalController.load(app.state.db,sid)
        tools=app.state.db.rows('SELECT * FROM tool_events ORDER BY started_at')
        artifact=Path('qa-reports/goal-live-trace.json')
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(json.dumps({'fixture':'ephemeral authorized loopback only',
            'ollama':'mocked metadata; no chat inference', 'session_status':session['status'],
            'task_goal':goal.model_dump() if goal else None,'tools':tools,'events':events,
            'requests':requests,'planted':planted},indent=2))
        assert session['status']=='completed', artifact
        assert goal.result['content']==planted
        unknown=any(f.get('kind')=='port' and f.get('service') in {'','unknown'} for f in json.loads(tools[0]['output_json'])['evidence'])
        expected=['net.scan_ports']+(['web.inspect'] if unknown else [])+['web.inspect','web.inspect','web.discover_paths','web.inspect']
        assert [r['tool_name'] for r in tools]==expected
        assert all(t['status']=='success' for t in tools)
        commands=[e['argv'] for e in events if e['type']=='tool_command']
        assert any(Path(c[0]).name=='nmap' for c in commands)
        fuzz=next(c for c in commands if Path(c[0]).name=='ffuf')
        assert fuzz[fuzz.index('-rate')+1]=='5' and fuzz[fuzz.index('-maxtime')+1]=='40'
        assert fuzz[fuzz.index('-t')+1]=='1'
        assert not any(r['tool_name'].startswith('ssh.') for r in tools)
        read=json.loads(tools[-1]['output_json'])
        assert planted in read['stdout']
        assert read['evidence'][-1]['url']==f'http://127.0.0.1:{port}/docs/flag.txt'
        counts=json.loads(app.state.db.one('SELECT counts_json FROM runs WHERE session_id=?',(sid,))['counts_json'])
        assert counts['tool_calls']==5+int(unknown) and counts['http_requests']==15+int(unknown)
        assert counts['model_turns']==0 and not mock_ollama.requests
    finally:
        server.close()
        await server.wait_closed()
