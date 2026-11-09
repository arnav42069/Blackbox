import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import pytest_asyncio
import yaml

from blackbox.core.scope import Scope, AuthorizationScope, approve_scope
from blackbox.tools.registry import ToolContext
from conftest import call
from test_api import new, finish


@pytest_asyncio.fixture
async def ssh_lab(settings,tmp_path,monkeypatch):
    monkeypatch.delenv('SSH_AUTH_SOCK',raising=False)
    root=tmp_path/'remote'
    home=root/'home'/'lab'
    home.mkdir(parents=True)
    (home/'user.txt').write_text('THM{local_fixture_read_only_flag}')
    (home/'.ssh').mkdir()
    (home/'.ssh'/'id_rsa').write_text('SHOULD NEVER BE READ')
    (home/'innocent.txt').symlink_to(home/'.ssh'/'id_rsa')
    key=asyncssh.generate_private_key('ssh-ed25519')
    server_key=asyncssh.generate_private_key('ssh-ed25519')
    keypath=tmp_path/'fixture_key'
    keypath.write_bytes(key.export_private_key())
    keypath.chmod(0o600)
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username): return True
        def public_key_auth_supported(self): return True
        def validate_public_key(self,username,public): return username=='lab' and public==key.convert_to_public()
    server=await asyncssh.create_server(Server,'127.0.0.1',0,config=None,rdns_lookup=False,gss_host=None,gss_auth=False,gss_kex=False,server_host_keys=[server_key],sftp_factory=lambda channel: asyncssh.SFTPServer(channel,chroot=str(root)))
    port=server.get_port()
    known=tmp_path/'known_hosts'
    known.write_bytes(f'[127.0.0.1]:{port} '.encode()+server_key.export_public_key())
    settings.config_dir.mkdir(exist_ok=True)
    profile=settings.config_dir/'ssh_profiles.yaml'
    profile.write_text(yaml.safe_dump({'fixture':{'host':'127.0.0.1','port':port,'username':'lab','key_path':str(keypath),'known_hosts':str(known),'allowed_read_roots':['/home']}}))
    profile.chmod(0o600)
    yield SimpleNamespace(port=port,known=known,key=key,profile=profile)
    server.close()
    await server.wait_closed()


async def test_agent_retrieves_real_sftp_flag(client,app,mock_ollama,ssh_lab):
    sid=await new(client)
    scope={'authorization_attested':True,'allowed_targets':['127.0.0.1/32'],'allowed_ports':str(ssh_lab.port),'allowed_remote_read_roots':['/home']}
    assert (await client.put(f'/api/sessions/{sid}/scope',json=scope)).status_code==200
    mock_ollama.replies=[call('ssh.probe',target='127.0.0.1',profile='fixture'),call('ssh.find_files',target='127.0.0.1',profile='fixture',path='/home',patterns=['user.txt']),call('ssh.read_file',target='127.0.0.1',profile='fixture',path='/home/lab/user.txt'),{'content':'Found flag from read-only fixture SFTP evidence'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Find the flag using my authorized fixture profile'})
    s=await finish(app,sid)
    assert s['status']=='completed', app.state.db.events(sid)
    tools=app.state.db.rows('SELECT * FROM tool_events')
    assert len(tools)==3 and all(t['status']=='success' for t in tools)
    read=json.loads(tools[-1]['output_json'])
    assert read['stdout']=='THM{local_fixture_read_only_flag}'
    assert read['evidence'][0]['path']=='/home/lab/user.txt'
    assert 'THM{local_fixture_read_only_flag}' in mock_ollama.requests[-1]['messages'][-1]['content']
    # A symlink with an innocuous name must not bypass secret path enforcement.
    mock_ollama.replies=[call('ssh.read_file',target='127.0.0.1',profile='fixture',path='/home/lab/innocent.txt'),{'content':'Cannot read secret stores'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'test denied symlink'})
    await finish(app,sid)
    latest=app.state.db.rows('SELECT * FROM tool_events ORDER BY started_at')[-1]
    assert latest['status']=='failed' and 'SHOULD NEVER' not in latest['output_json']


async def test_known_host_mismatch_is_not_bypassed(client,app,mock_ollama,ssh_lab):
    sid=await new(client)
    bad=asyncssh.generate_private_key('ssh-ed25519')
    ssh_lab.known.write_bytes(f'[127.0.0.1]:{ssh_lab.port} '.encode()+bad.export_public_key())
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(ssh_lab.port)})
    mock_ollama.replies=[call('ssh.probe',target='127.0.0.1',profile='fixture'),{'content':'Host-key verification blocked access'}]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Probe fixture'})
    await finish(app,sid)
    row=app.state.db.one('SELECT * FROM tool_events')
    assert row['status']=='failed' and 'host key verification failed' in row['output_json']


async def test_missing_profile_stops_with_needs_access(client,app,mock_ollama):
    sid=await new(client)
    await client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1']})
    mock_ollama.replies=[call('ssh.probe',target='127.0.0.1',profile='absent')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Use my profile'})
    assert (await finish(app,sid))['status']=='needs_access'
    assert 'Needs access' in app.state.db.messages(sid)[-1]['content']
