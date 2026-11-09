"""Opt-in local-only real Ollama smoke; no network target tools are requested."""
import asyncio
import io
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image

from blackbox.app import create_app
from blackbox.settings import Settings


async def main():
    model = sys.argv[1] if len(sys.argv)>1 else 'huihui_ai/qwen3.5-abliterated:4b'
    with tempfile.TemporaryDirectory(prefix='blackbox-real-') as directory:
        root=Path(directory)
        settings=Settings(data_dir=root/'data',config_dir=root/'config')
        settings.prefs.model=model
        app=create_app(settings)
        start=time.monotonic()
        try:
            probes=await app.state.ollama.probe(model)
            print('Real capability probes:',json.dumps(probes),flush=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765',headers={'Origin':'http://127.0.0.1:8765','Authorization':'Bearer '+app.state.auth.bearer}) as client:
                sid=(await client.post('/api/sessions',json={'model':model})).json()['id']
                image=io.BytesIO()
                Image.new('RGB',(96,96),'red').save(image,format='PNG')
                if probes.get('vision'):
                    r=await client.post(f'/api/sessions/{sid}/messages',data={'text':'What color fills the attached image? Reply with one short sentence.'},files={'images':('red.png',image.getvalue(),'image/png')})
                    assert r.status_code==200, r.text
                    await app.state.agent.tasks[sid]
                    answer=app.state.db.messages(sid)[-1]['content']
                    print('Real vision answer:',answer,flush=True)
                    assert app.state.db.session(sid)['status']=='completed' and 'red' in answer.lower()
                if probes.get('tools'):
                    r=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Call host.preflight now to check which host binaries actually exist, then briefly summarize its result. Use this tool; do not guess.'})
                    assert r.status_code==200, r.text
                    await app.state.agent.tasks[sid]
                    records=app.state.db.rows('SELECT tool_name,status FROM tool_events')
                    print('Real model tool executions:',json.dumps(records),flush=True)
                    print('Real model final answer:',app.state.db.messages(sid)[-1]['content'],flush=True)
                    assert app.state.db.session(sid)['status']=='completed', app.state.db.events(sid)
                    assert any(t['tool_name']=='host.preflight' and t['status']=='success' for t in records)
            print(f'Real smoke passed in {time.monotonic()-start:.1f}s',flush=True)
        finally:
            await app.state.agent.shutdown()
            await app.state.ollama.close()
            app.state.db.close()


if __name__=='__main__':
    asyncio.run(main())
