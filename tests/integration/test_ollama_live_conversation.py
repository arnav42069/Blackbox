"""Opt-in actual Ollama acceptance, with real scoped loopback HTTP and no public targets.

BLACKBOX_LIVE_OLLAMA_MODEL=huihui_ai/qwen3.5-abliterated:4b .venv/bin/python -m pytest -q -s tests/integration/test_ollama_live_conversation.py
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time

import httpx
import pytest

from blackbox.app import create_app
from blackbox.settings import Settings


MODEL=os.environ.get('BLACKBOX_LIVE_OLLAMA_MODEL')
pytestmark=pytest.mark.skipif(not MODEL,reason='Set BLACKBOX_LIVE_OLLAMA_MODEL for installed Ollama inference')


@pytest.mark.parametrize('task',['greeting','inspection'])
async def test_installed_model_streams_persisted_answer(tmp_path,task):
    settings=Settings(data_dir=tmp_path/'data',config_dir=tmp_path/'config')
    settings.prefs.model=MODEL
    settings.prefs.max_wall_seconds=180
    settings.prefs.max_turns=8
    settings.prefs.max_tool_calls=3
    # Exercise separate thinking frames as well as final content on the actual model.
    settings.prefs.think=True
    app=create_app(settings)
    db=app.state.db
    requests=[]
    marker='BlackBox controlled conversation fixture'
    async def handler(reader,writer):
        try:
            head=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
            requests.append(head.split(b'\r\n',1)[0].decode())
            body=f'<html><title>{marker}</title><body>Local test service, no vulnerability claim.</body></html>'.encode()
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server=await asyncio.start_server(handler,'127.0.0.1',0)
    try:
        port=server.sockets[0].getsockname()[1]
        cap=await app.state.ollama.capabilities(MODEL)
        # Copy capability probes only, by exact digest; never user history or scopes.
        actual=Settings().data_dir/'blackbox.db'
        if actual.exists():
            with sqlite3.connect(f'file:{actual}?mode=ro',uri=True) as source:
                row=source.execute('SELECT probe_json FROM model_capabilities WHERE model_digest=?',(cap['digest'],)).fetchone()
            if row:
                db.execute('UPDATE model_capabilities SET probe_json=? WHERE model_digest=?',(row[0],cap['digest']))
                app.state.ollama.cache=None
        cap=await app.state.ollama.capabilities(MODEL)
        assert cap['tools'], 'The installed model needs a successful tool probe for the inspection acceptance test'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765',headers={'Origin':'http://127.0.0.1:8765'}) as client:
            paired=await client.post('/api/pair',json={'token':app.state.auth.pairing})
            client.headers['Authorization']='Bearer '+paired.json()['token']
            sid=(await client.post('/api/sessions',json={'model':MODEL})).json()['id']
            url=f'http://127.0.0.1:{port}/'
            instruction='Hello, what can you do?'
            if task=='inspection':
                scope={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(port),'max_requests_per_run':3}
                assert (await client.put(f'/api/sessions/{sid}/scope',json=scope)).status_code==200
                instruction=f'Perform an authorized lab inspection of {url} using web.inspect once with GET, then give your final summary of the observed status and page title with evidence and limitations. Do not run host checks or scan other services.'
            started=time.monotonic()
            response=await client.post(f'/api/sessions/{sid}/messages',data={'text':instruction})
            assert response.status_code==200,response.text
            rid=response.json()['run_id']
            # Consume the production SSE endpoint while inference is running, rather than
            # deriving streaming success from a final session snapshot alone.
            route=next(r for r in app.routes if getattr(r,'path','')=='/api/sessions/{sid}/events')
            class Request:
                headers={}
                async def is_disconnected(self):
                    return False
            stream=await route.endpoint(sid,Request(),0)
            events=[]
            async with asyncio.timeout(190):
                async for frame in stream.body_iterator:
                    if 'data: ' not in frame:
                        continue
                    event=json.loads(frame.split('data: ',1)[1])
                    events.append(event)
                    if event['type']=='answer_delta':
                        assert db.session(sid)['status']=='running'
                        assert db.one('SELECT content FROM messages WHERE id=?',(event['message_id'],))['content']
                    if event['type'] in {'run_finished','run_failed','run_cancelled'}:
                        break
            await stream.body_iterator.aclose()
            await app.state.agent.tasks[sid]
            reloaded=(await client.get(f'/api/sessions/{sid}')).json()
            finals=[m for m in reloaded['messages'] if m['metadata'].get('conversation_kind')=='final']
            tools=db.rows('SELECT id,tool_name,status,output_json FROM tool_events WHERE run_id=?',(rid,))
            report={'model':MODEL,'digest':cap['digest'],'task':task,'seconds':round(time.monotonic()-started,2),
                    'status':reloaded['status'],'final':finals[-1]['content'] if finals else None,
                    'streamed_deltas':sum(e['type']=='answer_delta' for e in events),
                    'response_diagnostics':[e for e in events if e['type']=='model_response'],
                    'tools':[{'id':t['id'],'tool':t['tool_name'],'status':t['status']} for t in tools],
                    'http_requests':requests,
                    'terminal_events':[e['type'] for e in events if e['type'] in {'run_finished','run_failed','run_cancelled'}]}
            artifact=Path('qa-reports')/f'ollama-live-{task}.json'
            artifact.parent.mkdir(exist_ok=True)
            artifact.write_text(json.dumps(report,indent=2))
            print(json.dumps(report,indent=2),flush=True)
            assert reloaded['status']=='completed',artifact
            assert finals and finals[-1]['content'].strip()
            assert report['streamed_deltas']>1
            assert report['terminal_events']==['run_finished']
            assert events[-1]['event_id']>next(e['event_id'] for e in reversed(events) if e['type']=='assistant_message' and e['metadata']['conversation_kind']=='final')
            assert not any('thinking' in m for m in await app.state.agent.history(sid))
            if task=='greeting':
                assert not tools and not requests
            else:
                assert tools and all(t['tool_name']=='web.inspect' and t['status']=='success' for t in tools)
                assert 1<=len(requests)<=3
                assert any(e['status']==200 for t in tools for e in json.loads(t['output_json'])['evidence'])
                assert '200' in report['final'] and marker in report['final']
                assert all(t['id'] in report['final'] for t in tools)
                assert 'elapsed' not in report['final'].lower() and '6\u202fms' not in report['final']
                observations=[json.loads(m['content']) for m in db.messages(sid) if m['role']=='tool']
                assert all(o['observed_at_utc'] in report['final'] for o in observations)
    finally:
        server.close()
        await server.wait_closed()
        await app.state.agent.shutdown()
        await app.state.ollama.close()
        db.close()
