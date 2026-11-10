"""Only disposable backends with explicit temporary data directories are signalled."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest

from blackbox import processes as manager


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def environment():
    env={**os.environ,'PYTHONPATH':str(manager.PACKAGE.parent)}
    env.pop('BLACKBOX_READY_FD',None)
    return env


def command(data,*args,timeout=15):
    # No test is permitted to perform unscoped discovery-and-kill of developer data.
    assert str(data).startswith('/tmp/')
    return subprocess.run([sys.executable,'-m','blackbox',*args,'--data-dir',str(data)],
                          cwd='/',env=environment(),capture_output=True,text=True,timeout=timeout)


def wait_ready(port,process,log):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise AssertionError(log.read_text())
        try:
            response=httpx.get(f'http://127.0.0.1:{port}/api/health',trust_env=False,timeout=.2)
            if response.status_code==200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.05)
    raise AssertionError('Disposable backend did not become ready: '+log.read_text())


@pytest.fixture
def backends(tmp_path):
    processes=[]
    def start(name='one',model='fixture:original',extra=(),startup_command=(),python=None,env_extra=None,ready=True):
        data=tmp_path/name/'data'
        config=tmp_path/name/'config'
        log=tmp_path/(name+'.log')
        port=free_port()
        env=environment()
        env.update(env_extra or {})
        with log.open('w') as output:
            process=subprocess.Popen([python or sys.executable,'-m','blackbox',*startup_command,'--no-browser','--data-dir',str(data),
                '--config-dir',str(config),'--port',str(port),'--model',model,*extra],
                cwd='/',env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        log.chmod(0o600)
        processes.append((data,process))
        if ready:
            wait_ready(port,process,log)
        return data,config,port,process,log
    yield start
    for data,process in reversed(processes):
        try:
            result=command(data,'kill')
            if result.returncode:
                # Only our still-owned subprocess, never a global process group.
                if process.poll() is None:
                    process.terminate()
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            assert not manager.discover(data), 'Disposable backend was orphaned'


def pair_url(log):
    return re.search(r'http://127\.0\.0\.1:\d+/#pair=([^\s]+)',log.read_text())[1]


def test_real_kill_from_unrelated_directory(backends):
    data,_,port,process,_=backends()
    assert manager.select_instance(data).process.pid==process.pid
    result=command(data,'kill')
    assert result.returncode==0,result.stderr
    assert f'PID {process.pid}' in result.stdout and 'SIGTERM required' in result.stdout
    process.wait(timeout=5)
    assert manager.port_free(port)
    assert (data/'server.lock').exists()  # Stable lock inode is never unlinked.
    assert not (data/'instance.json').exists()
    assert 'No running BlackBox instance found' in command(data,'kill').stdout


def test_restart_preserves_config_fresh_auth_and_revokes_scope(backends):
    data,config,port,old,log=backends()
    token=pair_url(log)
    base=f'http://127.0.0.1:{port}'
    with httpx.Client(base_url=base,trust_env=False,headers={'Origin':base}) as client:
        bearer=client.post('/api/pair',json={'token':token}).json()['token']
        client.headers['Authorization']='Bearer '+bearer
        sid=client.post('/api/sessions',json={'model':'fixture:original'}).json()['id']
        assert client.put(f'/api/sessions/{sid}/scope',json={'authorization_attested':True,'allowed_targets':['127.0.0.1'],'allowed_ports':str(port)}).status_code==200
    result=command(data,'restart')
    assert result.returncode==0,result.stderr+result.stdout
    old.wait(timeout=5)
    fresh=manager.select_instance(data)
    assert fresh.process.pid!=old.pid
    assert fresh.config=={'model':'fixture:original','port':port,'data_dir':str(data),
        'config_dir':str(config),'no_browser':True,'ollama_url':'http://127.0.0.1:11434'}
    new_token=re.search(r'#pair=([^\s]+)',result.stdout)[1]
    assert new_token!=token and token not in result.stdout
    assert str(fresh.process.pid) in result.stdout
    with httpx.Client(base_url=base,trust_env=False,headers={'Origin':base}) as client:
        assert client.post('/api/pair',json={'token':token}).status_code==401
        new_bearer=client.post('/api/pair',json={'token':new_token}).json()['token']
        assert new_bearer!=bearer
        client.headers['Authorization']='Bearer '+new_bearer
        assert client.get(f'/api/sessions/{sid}').json()['scope'] is None
    assert '#pair=' not in (data/'server.log').read_text()
    assert 'pairing' not in (data/'instance.json').read_text()
    assert len(manager.discover(data))==1


def test_restart_override_model_and_port(backends):
    data,_,old_port,old,_=backends()
    port=free_port()
    result=command(data,'restart','--model','huihui_ai/qwen3.5-abliterated:4b','--port',str(port))
    assert result.returncode==0,result.stderr
    old.wait(timeout=5)
    new=manager.select_instance(data)
    assert new.config['model']=='huihui_ai/qwen3.5-abliterated:4b'
    assert new.config['port']==port and manager.port_free(old_port)


def test_restart_without_instance_runs_foreground(backends):
    data,_,port,process,log=backends(startup_command=('restart',))
    assert manager.select_instance(data).process.pid==process.pid
    assert pair_url(log)
    assert not manager.port_free(port)


def test_multiple_instances_targeted_stop(backends):
    first=backends('first')
    second=backends('second')
    result=command(first[0],'kill')
    assert result.returncode==0
    first[3].wait(timeout=5)
    assert second[3].poll() is None and manager.select_instance(second[0]).process.pid==second[3].pid


def test_legacy_backend_without_record_is_proven_by_lock(backends):
    data,_,_,process,_=backends()
    (data/'instance.json').unlink()
    instance=manager.select_instance(data)
    assert instance.process.pid==process.pid
    assert instance.config['model']=='fixture:original'
    assert command(data,'kill').returncode==0
    process.wait(timeout=5)


def test_stale_pid_and_lock_files(tmp_path):
    data=tmp_path/'data'
    data.mkdir(mode=0o700)
    manager.atomic_write(data/'instance.json',json.dumps({'pid':99999999,'start':'stale','source':str(manager.PACKAGE),'data_dir':str(data)}).encode())
    result=command(data,'kill')
    assert result.returncode==0 and 'No running' in result.stdout
    assert not (data/'instance.json').exists()
    assert (data/'server.lock').exists()


def test_unrelated_process_holding_named_lock_never_killed(tmp_path):
    data=tmp_path/'data'
    data.mkdir(mode=0o700)
    code="import fcntl,os,sys,time; f=open(sys.argv[-1]+'/server.lock','w'); os.chmod(f.name,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)"
    process=subprocess.Popen([sys.executable,'-c',code,'uvicorn','blackbox',str(data)],stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        result=command(data,'kill')
        assert result.returncode!=0 and 'holds' in result.stderr
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_restart_port_conflict_preserves_running_backend(backends):
    data,_,_,old,_=backends()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        sock.listen()
        result=command(data,'restart','--port',str(sock.getsockname()[1]))
        assert result.returncode!=0 and 'still busy' in result.stderr
        assert old.poll() is None


def test_startup_failure_has_no_child_orphan(backends):
    data,config,_,old,_=backends()
    # Parent can capture old config, but the fresh interpreter must reject invalid preferences.
    config.mkdir(exist_ok=True)
    (config/'config.yaml').write_text('max_tool_calls: not-an-integer\n')
    result=command(data,'restart')
    assert result.returncode!=0 and 'before readiness' in result.stderr.lower()
    old.wait(timeout=5)
    assert not manager.discover(data)
    with manager.file_lock(data/'server.lock'):
        pass


def test_real_sigkill_escalation(backends):
    import signal
    data,_,port,process,_=backends()
    os.kill(process.pid,signal.SIGSTOP)  # Only the disposable test-owned backend.
    started=time.monotonic()
    result=command(data,'kill')
    assert result.returncode==0,result.stderr
    assert 'SIGKILL required' in result.stdout
    assert time.monotonic()-started>=3
    assert process.wait(timeout=5)==-signal.SIGKILL
    assert manager.port_free(port)


def test_concurrent_restart_does_not_duplicate(backends):
    data,_,_,old,_=backends()
    argv=[sys.executable,'-m','blackbox','restart','--data-dir',str(data)]
    first=subprocess.Popen(argv,cwd='/',env=environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    second=subprocess.Popen(argv,cwd='/',env=environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        first.communicate(timeout=15)
        second.communicate(timeout=15)
        assert 0 in [first.returncode,second.returncode]
        old.wait(timeout=5)
        assert len(manager.discover(data))==1
    finally:
        for child in [first,second]:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


def test_help_and_offline_doctor_are_backward_compatible(tmp_path):
    data=tmp_path/'data'
    result=command(data,'--help')
    assert result.returncode==0 and '{doctor,kill,restart}' in result.stdout
    assert not data.exists()
    # Own this port without listening: connection refused, no unrelated Ollama contacted.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        env=environment()
        env['BLACKBOX_OLLAMA_URL']=f'http://127.0.0.1:{sock.getsockname()[1]}'
        result=subprocess.run([sys.executable,'-m','blackbox','doctor','--json','--data-dir',str(data),
            '--config-dir',str(tmp_path/'config')],env=env,cwd='/',text=True,capture_output=True,timeout=10)
        assert result.returncode==0,result.stderr
        payload=json.loads(result.stdout)
        assert 'dependencies' in payload and 'Ollama unavailable' in payload['ollama']
    assert not manager.discover(data)


def test_interpreter_alias_can_stop_occupied_server_lock(backends):
    alias=str(Path(sys.executable).with_name('python3'))
    data,_,_,process,_=backends(python=alias)
    result=command(data,'kill')
    assert result.returncode==0,result.stderr
    process.wait(timeout=5)
    assert 'SIGTERM required' in result.stdout


@pytest.mark.parametrize('operation',['kill','restart'])
def test_stale_registration_does_not_block_verified_lock_owner(backends,operation):
    data,_,port,process,_=backends()
    path=data/'instance.json'
    record=json.loads(path.read_text())
    record.update(pid=99999999,start='obsolete-registration')
    manager.atomic_write(path,json.dumps(record).encode())
    result=command(data,operation)
    assert result.returncode==0,result.stderr
    assert str(process.pid) in result.stdout
    process.wait(timeout=5)
    assert manager.port_free(port) if operation=='kill' else len(manager.discover(data))==1


def startup_hook(tmp_path,body):
    folder=tmp_path/'hook'
    folder.mkdir()
    (folder/'sitecustomize.py').write_text('from blackbox import app\nimport os,fcntl,time\n_original=app.create_app\ndef create_app(settings=None,*args,**kwargs):\n'+body+'\n    return _original(settings,*args,**kwargs)\napp.create_app=create_app\n')
    return {'PYTHONPATH':str(folder)+os.pathsep+str(manager.PACKAGE.parent)}


@pytest.mark.parametrize('operation',['kill','restart'])
def test_starting_backend_does_not_retain_lifecycle_lock(backends,tmp_path,operation):
    env=startup_hook(tmp_path,"    (settings.data_dir/'startup-pending').write_text('ready')\n    time.sleep(30)")
    data,_,port,process,_=backends(env_extra=env,ready=False)
    deadline=time.monotonic()+5
    while not (data/'startup-pending').exists():
        assert process.poll() is None
        assert time.monotonic()<deadline
        time.sleep(.02)
    assert manager.select_instance(data).process.pid==process.pid
    with manager.file_lock(data/'lifecycle.lock'):
        pass
    result=command(data,operation)
    assert result.returncode==0,result.stderr
    process.wait(timeout=5)
    assert manager.port_free(port) if operation=='kill' else len(manager.discover(data))==1


def test_legacy_backend_holding_old_control_lock_can_be_stopped(backends,tmp_path):
    env=startup_hook(tmp_path,"    fd=os.open(settings.data_dir/'control.lock',os.O_RDWR|os.O_CREAT,0o600)\n    fcntl.flock(fd,fcntl.LOCK_EX)\n    globals()['_legacy_fd']=fd")
    data,_,_,process,_=backends(env_extra=env)
    assert manager.lock_owned(data/'control.lock',process.pid)
    result=command(data,'kill')
    assert result.returncode==0,result.stderr
    process.wait(timeout=5)
    assert (data/'server.lock').exists() and (data/'control.lock').exists()


def test_kill_waits_for_concurrent_restart_without_duplicate(backends):
    import signal
    data,_,port,old,_=backends()
    os.kill(old.pid,signal.SIGSTOP)
    first=subprocess.Popen([sys.executable,'-m','blackbox','restart','--data-dir',str(data)],cwd='/',env=environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    second=None
    try:
        deadline=time.monotonic()+2
        while first.pid not in [owner.pid for owner in manager.lock_holders(data/'lifecycle.lock')]:
            assert first.poll() is None and time.monotonic()<deadline
            time.sleep(.02)
        second=subprocess.Popen([sys.executable,'-m','blackbox','kill','--data-dir',str(data)],cwd='/',env=environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        out,err=first.communicate(timeout=15)
        assert first.returncode==0,err+out
        out,err=second.communicate(timeout=15)
        assert second.returncode==0,err+out
        old.wait(timeout=3)
        assert not manager.discover(data) and manager.port_free(port)
    finally:
        for child in [first,second]:
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=3)
