"""Explicit local test fixture; never loaded by the production CLI."""
import asyncio
import base64
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
import uvicorn
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from conftest import MockOllama, call
from blackbox.app import create_app
from blackbox.settings import Settings
from blackbox.storage.files import atomic_write

class Fixture(MockOllama):
    def __call__(self,request):
        if request.url.path=='/api/chat':
            body=json.loads(request.content)
            if body.get('tools', [{}])[0].get('function', {}).get('name')=='agent.review_completion':
                return super().__call__(request)
            if body['messages'][0].get('content', '').startswith('BLACKBOX_IMAGE_OBSERVATIONS:'):
                return super().__call__(request)
            messages=body['messages']
            latest=max(i for i,m in enumerate(messages) if m['role']=='user')
            recent=messages[latest:]
            prompt=recent[0]['content'].lower()
            if 'workspace recovery' in prompt:
                count=sum(m['role']=='tool' for m in recent)
                steps=[call('workspace.read',path='/home/lab/flag.txt'),call('workspace.read',path='/home/lab/flag.txt'),
                       call('workspace.list',path='.'),call('workspace.read',path='recovery-notes.txt'),
                       {'content':'Read the actual local notes: controlled workspace evidence. No remote file access was established.'}]
                self.replies.append(steps[min(count,len(steps)-1)])
                return super().__call__(request)
            if not any(m['role']=='tool' for m in recent):
                if 'preflight' in prompt or 'structured image observations' in prompt:
                    self.replies.append(call('host.preflight'))
                elif 'authorized lab inspection' in prompt:
                    self.replies.append(call('web.inspect',url='http://127.0.0.1:8080/'))
                elif 'outside scope' in prompt:
                    self.replies.append(call('web.inspect',url='http://8.8.8.8/'))
            elif 'authorized lab inspection' in prompt:
                result=json.loads(recent[-1]['content'])
                self.replies.append({'content':f"Inspected the authorized lab. HTTP **200** was observed at `http://127.0.0.1:8080/`. Evidence: {result['tool_id']}. No vulnerability is verified.\n\n| Observation | Value |\n| --- | --- |\n| Status | 200 |\n\n```python\nprint('lab')\n```\nNext step: review the application within scope."})
            elif 'preflight' in prompt or 'structured image observations' in prompt:
                result=json.loads(recent[-1]['content'])
                assert result['ok'] and result['evidence'][0]['os']=='Linux'
                if recent[0].get('images'):
                    assert base64.b64decode(recent[0]['images'][0]).startswith(b'\x89PNG')
                self.replies.append({'content':'Verified host preflight complete. Image bytes received.'})
        return super().__call__(request)

class SlowTransport(httpx.AsyncBaseTransport):
    def __init__(self): self.fixture=Fixture()
    async def handle_async_request(self,request):
        if request.url.path=='/api/chat' and b'slow request' in request.content:
            await asyncio.sleep(60)
        response = self.fixture(request)
        if request.url.path=='/api/chat' and json.loads(request.content).get('stream'):
            message=response.json()['message']
            class Tokens(httpx.AsyncByteStream):
                async def __aiter__(self):
                    content=message.get('content','')
                    if message.get('tool_calls'):
                        yield (json.dumps({'message':message,'done':False})+'\n').encode()
                    else:
                        for offset in range(0,len(content),24):
                            yield (json.dumps({'message':{'content':content[offset:offset+24]},'done':False})+'\n').encode()
                            await asyncio.sleep(.06)
                    yield b'{"done":true}\n'
            return httpx.Response(200,stream=Tokens())
        return response

if __name__=='__main__':
    temporary = tempfile.TemporaryDirectory(prefix='blackbox-e2e-')
    with temporary as root:
        path=Path(root)
        port=int(os.environ.get('BLACKBOX_E2E_PORT', '8876'))
        settings=Settings(port=port,data_dir=path/'data',config_dir=path/'config')
        settings.prefs.model='fixture:4b'
        app=create_app(settings,SlowTransport())
        (settings.data_dir/'workspace/recovery-notes.txt').write_text('controlled workspace evidence')
        # Mock execution only; real schema, policy, destination and evidence paths remain active.
        from blackbox.tools.base import ToolResult
        async def lab_inspect(ctx,args):
            if ctx.intent=='goal':
                ctx.request()
                return ToolResult(ok=True,summary='Controlled goal fixture HTTP 200',stdout='THM{browser_goal_evidence}' if args.url.endswith('/flag.txt') else 'Controlled lab index',evidence=[{'url':args.url,'status':200}])
            return ToolResult(ok=True,summary='Lab fixture returned HTTP 200',stdout='Authorized lab fixture',
                evidence=[{'url':args.url,'status':200,'headers':{'server':'lab-fixture'}}])
        app.state.agent.registry.specs['web.inspect'].run=lab_inspect
        from blackbox.tools import network
        async def route_fixture(ctx,target):
            return ToolResult(ok=True,summary='Mock loopback route and reachability',evidence=[{'target':target,'interface':'lo'}])
        network.preflight=route_fixture
        runtime=Path(os.environ.get('BLACKBOX_E2E_RUNTIME', str(Path(__file__).resolve().parents[2]/'.runtime')))
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.mkdir(exist_ok=True)
        (runtime/'e2e-bearer').unlink(missing_ok=True)
        atomic_write(runtime/'e2e-pair',app.state.auth.pairing.encode())
        original_lifespan = app.router.lifespan_context
        @asynccontextmanager
        async def evidence_lifespan(application):
            try:
                async with original_lifespan(application):
                    yield
            finally:
                # Uvicorn re-raises SIGTERM after lifespan shutdown; cleanup after
                # uvicorn.run() may never execute. Export while shutdown owns control.
                try:
                    if report := os.environ.get('BLACKBOX_QA_REPORT'):
                        from scripts.qa_support import write_json
                        import sqlite3
                        connection=sqlite3.connect(f'file:{settings.data_dir}/blackbox.db?mode=ro', uri=True)
                        try:
                            connection.row_factory=sqlite3.Row
                            write_json(Path(report)/'browser-tool-journal.json', {
                                'source':'isolated_browser_fixture',
                                'tools':[dict(row) for row in connection.execute('SELECT * FROM tool_events')],
                                'events':[dict(row) for row in connection.execute('SELECT * FROM event_journal')]})
                        finally:
                            connection.close()
                finally:
                    (runtime/'e2e-bearer').unlink(missing_ok=True)
                    (runtime/'e2e-pair').unlink(missing_ok=True)
                    temporary.cleanup()
        app.router.lifespan_context = evidence_lifespan
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning',access_log=False)
