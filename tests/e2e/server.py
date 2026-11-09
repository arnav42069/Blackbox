"""Explicit local test fixture; never loaded by the production CLI."""
import asyncio
import base64
import json
import os
import sys
import tempfile
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
            messages=body['messages']
            latest=max(i for i,m in enumerate(messages) if m['role']=='user')
            recent=messages[latest:]
            prompt=recent[0]['content'].lower()
            if not any(m['role']=='tool' for m in recent):
                if 'preflight' in prompt or recent[0].get('images'):
                    self.replies.append(call('host.preflight'))
                elif 'outside scope' in prompt:
                    self.replies.append(call('web.inspect',url='http://8.8.8.8/'))
            elif 'preflight' in prompt or recent[0].get('images'):
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
        return self.fixture(request)

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='blackbox-e2e-') as root:
        path=Path(root)
        port=int(os.environ.get('BLACKBOX_E2E_PORT', '8876'))
        settings=Settings(port=port,data_dir=path/'data',config_dir=path/'config')
        settings.prefs.model='fixture:4b'
        app=create_app(settings,SlowTransport())
        runtime=Path(os.environ.get('BLACKBOX_E2E_RUNTIME', str(Path(__file__).resolve().parents[2]/'.runtime')))
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.mkdir(exist_ok=True)
        (runtime/'e2e-bearer').unlink(missing_ok=True)
        atomic_write(runtime/'e2e-pair',app.state.auth.pairing.encode())
        try:
            uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning',access_log=False)
        finally:
            if report := os.environ.get('BLACKBOX_QA_REPORT'):
                from scripts.qa_support import write_json
                # Uvicorn lifespan closed the DB: use a fresh read-only connection.
                import sqlite3
                connection=sqlite3.connect(f'file:{settings.data_dir}/blackbox.db?mode=ro', uri=True)
                connection.row_factory=sqlite3.Row
                write_json(Path(report)/'browser-tool-journal.json', {
                    'source':'isolated_browser_fixture',
                    'tools':[dict(row) for row in connection.execute('SELECT * FROM tool_events')],
                    'events':[dict(row) for row in connection.execute('SELECT * FROM event_journal')]})
                connection.close()
            (runtime/'e2e-bearer').unlink(missing_ok=True)
            (runtime/'e2e-pair').unlink(missing_ok=True)
