#!/usr/bin/env python3
"""Reproducible loopback-only demo. Vision is mocked; HTTP and ffuf are real.

Run: .venv/bin/python scripts/demo_orchestration.py --output qa-reports/orchestration-demo
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from blackbox.app import create_app
from blackbox.settings import Settings


async def demo(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    requests, vision_calls = [], []

    async def handler(reader, writer):
        try:
            request = await reader.readuntil(b'\r\n\r\n')
            path = request.split(b' ')[1].decode()
            status = 200 if path in {'/', '/health', '/robots.txt'} else 403 if path=='/admin' else 404
            requests.append({'path':path, 'status':status, 'at':time.monotonic()})
            body = b'BlackBox controlled local fixture'
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    transcript = f'Nmap scan report for 127.0.0.1\nPORT      STATE SERVICE\n{port}/tcp open  http\n22/tcp closed ssh'
    image = Image.new('RGB', (1000,220), 'black')
    ImageDraw.Draw(image).multiline_text((20,20), transcript, fill='white', font_size=24)
    image.save(output/'screenshot.png')
    png = io.BytesIO()
    image.save(png, format='PNG')

    def ollama(request):
        if request.url.path=='/api/tags':
            return httpx.Response(200,json={'models':[{'name':'text-fixture','digest':'demo-text'},{'name':'vision-fixture','digest':'demo-vision'}]})
        body=json.loads(request.content)
        if request.url.path=='/api/show':
            return httpx.Response(200,json={'capabilities':['completion']+(['vision'] if body['model']=='vision-fixture' else [])})
        assert body['model']=='vision-fixture' and body['messages'][0]['images']
        vision_calls.append(body['model'])
        return httpx.Response(200,json={'message':{'content':json.dumps({'category':'scan','visible_text':transcript,'uncertainties':['Original scan timestamp is not visible.']})}})

    try:
        with tempfile.TemporaryDirectory(prefix='blackbox-demo-') as folder:
            settings=Settings(data_dir=Path(folder)/'data',config_dir=Path(folder)/'config')
            settings.prefs.vision_model='vision-fixture'
            settings.prefs.http_rate=5
            app=create_app(settings,httpx.MockTransport(ollama))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765',headers={'Origin':'http://127.0.0.1:8765'}) as client:
                    token=(await client.post('/api/pair',json={'token':app.state.auth.pairing})).json()['token']
                    client.headers['Authorization']='Bearer '+token
                    sid=(await client.post('/api/sessions',json={'model':'text-fixture'})).json()['id']
                    response=await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(port),'allow_active_scanning':True,'max_requests_per_run':24})
                    response.raise_for_status()
                    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Read this screenshot'},files={'images':('nmap.png',png.getvalue(),'image/png')})
                    response.raise_for_status()
                    await app.state.agent.tasks[sid]
                    assert app.state.db.session(sid)['status']=='completed'
                    assert not requests
                    started=time.monotonic()
                    for _ in range(2):
                        response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Fuzz the web server.'})
                        response.raise_for_status()
                        await app.state.agent.tasks[sid]
                        assert app.state.db.session(sid)['status']=='completed', app.state.db.events(sid)
                    elapsed=time.monotonic()-started
                    events=app.state.db.events(sid)
                    scans=app.state.db.rows('SELECT * FROM scan_results WHERE session_id=? ORDER BY stored_at',(sid,))
                    tool_events=[e for e in events if e['type']=='tool_finished']
                    assert len(requests)==12 and len(vision_calls)==1
                    assert len(scans)==2 and tool_events[-1]['cached_scan_id']
                    assert not any(e.get('name','').startswith('ssh.') or e.get('name')=='net.scan_ports' for e in tool_events)
                    answer=app.state.db.messages(sid)[-1]['content']
                    payload={'vision':'mocked deterministic transcription of generated screenshot', 'http':'real loopback fixture', 'executor':'real ffuf', 'elapsed_seconds':round(elapsed,3), 'request_count':len(requests), 'vision_calls':len(vision_calls), 'requests':requests, 'events':events, 'scan_records':scans, 'answer':answer}
                    (output/'trace.json').write_text(json.dumps(payload,indent=2))
                    summary=f'''# Verified local orchestration demo

Vision: mocked transcription; selected chat model is text-only and routes images to configured vision-fixture.
HTTP: real loopback fixture on port {port}. Executor: real ffuf.

- Screenshot transcribed into port observations; port {port} is visibly HTTP.
- Original scan time is unknown; screenshot is marked unverified, not a live scan.
- “Fuzz the web server” selects web.discover_paths / ffuf without Nmap or SSH.
- 12 requests executed at a configured maximum of 5 requests/second; timeout 40 seconds, concurrency 1, budget 24.
- Actual HTTP codes: {sorted({r['status'] for r in requests})}.
- Two structured records saved: visual observations and successful HTTP discovery.
- Repeating the request reuses the successful result: 12 total HTTP requests, one vision call.
- Elapsed discovery and reuse: {elapsed:.3f} seconds.

Artifacts: screenshot.png, trace.json (plans, argv, evidence, results and cache trace).
'''
                    (output/'report.md').write_text(summary)
                    print(summary)
            finally:
                await app.state.agent.shutdown()
                await app.state.ollama.close()
                app.state.db.close()
    finally:
        server.close()
        await server.wait_closed()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('qa-reports/orchestration-demo'))
    asyncio.run(demo(parser.parse_args().output))
