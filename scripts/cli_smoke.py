"""Verify the installed launcher over loopback with entirely temporary app data."""
import asyncio
import os
import re
import signal
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx


async def main():
    with tempfile.TemporaryDirectory(prefix='blackbox-cli-') as root:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        env={**os.environ,'BLACKBOX_DATA_DIR':str(Path(root)/'data'),'BLACKBOX_CONFIG_DIR':str(Path(root)/'config')}
        launcher=Path.home()/'.local/bin/blackbox'
        process=subprocess.Popen([str(launcher),'--no-browser','--port',str(port)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        try:
            url=f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient(base_url=url,trust_env=False,timeout=3) as client:
                for _ in range(100):
                    try:
                        if (await client.get('/api/health')).status_code==200:
                            break
                    except httpx.ConnectError:
                        await asyncio.sleep(.05)
                else:
                    raise RuntimeError('CLI server did not become ready')
                index=await client.get('/')
                assert index.status_code==200 and 'BLACKBOX' in index.text.upper()
                assert "default-src 'self'" in index.headers['content-security-policy']
                lines=[process.stdout.readline() for _ in range(3)]
                pairing=re.search(r'#pair=([^\s]+)',''.join(lines)).group(1)
                client.headers['Origin']=url
                paired=await client.post('/api/pair',json={'token':pairing})
                assert paired.status_code==200
                client.headers['Authorization']='Bearer '+paired.json()['token']
                assert (await client.get('/api/models')).status_code==200
                assert (await client.get('/api/preflight')).json()['ollama']['connected']
                created=await client.post('/api/sessions',json={})
                assert created.status_code==200
                print('Installed CLI: health, built UI, pairing, auth, real Ollama discovery and session creation PASS')
                status=Path(f'/proc/{process.pid}/status').read_text()
                rss=re.search(r'VmRSS:\s*(\d+)',status)
                if rss:
                    print(f'Idle backend RSS after discovery: {int(rss.group(1))/1024:.1f} MiB')
        finally:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()


if __name__=='__main__':
    asyncio.run(main())
