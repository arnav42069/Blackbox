import json
import os
from pathlib import Path
import signal
import sys

import pytest

from blackbox import processes as p


@pytest.fixture
def instance(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    proc=p.Process(99999999,'42',os.readlink('/proc/self/exe'),(sys.executable,'-m','blackbox','--no-browser'),1)
    config={'data_dir':str(tmp_path),'config_dir':str(tmp_path/'config'),'model':'fixture:model','port':18765,'no_browser':True,'ollama_url':'http://127.0.0.1:11434'}
    record={'pid':proc.pid,'start':proc.start,'executable':proc.executable,'data_dir':str(tmp_path),'source':str(p.PACKAGE),'config':config}
    p.atomic_write(p.record_path(tmp_path),json.dumps(record).encode())
    monkeypatch.setattr(p,'inspect',lambda pid:proc)
    monkeypatch.setattr(p,'lock_owned',lambda path,pid:True)
    return p.Instance(proc,tmp_path,config)


def mock_stop(monkeypatch,instance,exits):
    signals=[]
    monkeypatch.setattr(p,'tool_handles',lambda parent:[])
    monkeypatch.setattr(p.os,'pidfd_open',lambda pid:os.open('/dev/null',os.O_RDONLY))
    monkeypatch.setattr(p,'signal_fd',lambda fd,sig:signals.append(sig))
    remaining=iter(exits)
    monkeypatch.setattr(p,'exited',lambda fd,timeout:next(remaining))
    return signals


def test_graceful_shutdown(instance,monkeypatch):
    signals=mock_stop(monkeypatch,instance,[False,True])
    assert p.stop(instance,lambda msg:None)=='SIGTERM'
    assert signals==[signal.SIGTERM]


def test_forced_shutdown(instance,monkeypatch):
    signals=mock_stop(monkeypatch,instance,[False,False,True])
    assert p.stop(instance,lambda msg:None)=='SIGKILL'
    assert signals==[signal.SIGTERM,signal.SIGKILL]


def test_reused_pid_never_signalled(instance,monkeypatch):
    from dataclasses import replace
    signals=mock_stop(monkeypatch,instance,[False])
    monkeypatch.setattr(p,'inspect',lambda pid:replace(instance.process,start='other-start'))
    with pytest.raises(p.InstanceError,match='identity changed'):
        p.stop(instance)
    assert signals==[]


def test_permission_failure_no_escalation(instance,monkeypatch):
    signals=mock_stop(monkeypatch,instance,[False])
    def denied(fd,sig):
        signals.append(sig)
        raise PermissionError('denied')
    monkeypatch.setattr(p,'signal_fd',denied)
    with pytest.raises(p.InstanceError,match='Permission denied'):
        p.stop(instance)
    assert signals==[signal.SIGTERM]


def test_already_exited_and_nonexistent(instance,monkeypatch):
    assert mock_stop(monkeypatch,instance,[True])==[]
    assert p.stop(instance)=='already stopped'
    def gone(pid):
        raise ProcessLookupError()
    monkeypatch.setattr(p.os,'pidfd_open',gone)
    assert p.stop(instance)=='already stopped'


@pytest.mark.parametrize('argv',[
    (sys.executable,'-m','uvicorn','blackbox.app:create_app'),
    (sys.executable,'other.py','blackbox','uvicorn'),
    (sys.executable,'-c','print("blackbox")'),
    (sys.executable,'-m','blackbox','kill'),
    (sys.executable,'-m','blackbox','doctor'),
    (sys.executable,'-m','blackbox','--help'),
    (sys.executable,'/tmp/blackbox'),
])
def test_unrelated_commands_rejected(argv):
    assert not p.backend_command(argv)


def test_module_and_installed_launcher_recognized():
    assert p.backend_command((sys.executable,'-m','blackbox','--model','doctor'))
    assert p.backend_command((sys.executable,'-m','blackbox','restart','--no-browser'))
    assert p.backend_command((sys.executable,str(Path(sys.executable).parent/'blackbox'),'--port=9876'))


def test_stale_record_cleaned_without_deleting_lock(tmp_path):
    tmp_path.chmod(0o700)
    p.atomic_write(p.record_path(tmp_path),json.dumps({'pid':99999999,'source':str(p.PACKAGE),'data_dir':str(tmp_path)}).encode())
    p.clean_stale(tmp_path)
    assert not p.record_path(tmp_path).exists()
    assert (tmp_path/'server.lock').exists()


def test_active_lock_and_symlinks_never_deleted(tmp_path):
    tmp_path.chmod(0o700)
    p.atomic_write(p.record_path(tmp_path),b'{}')
    with p.file_lock(tmp_path/'server.lock'):
        with pytest.raises(p.InstanceError,match='holds'):
            p.clean_stale(tmp_path)
    assert p.record_path(tmp_path).exists()
    (tmp_path/'server.lock').unlink()
    (tmp_path/'server.lock').symlink_to(tmp_path/'instance.json')
    with pytest.raises(OSError):
        p.clean_stale(tmp_path)
    assert p.record_path(tmp_path).exists()


def test_stale_pid_record_cannot_identify_other_process(instance):
    path=p.record_path(instance.data)
    record=json.loads(path.read_text())
    record['start']='stale'
    p.atomic_write(path,json.dumps(record).encode())
    with pytest.raises(p.InstanceError,match='stale PID'):
        p.validate(instance.process,instance.data)


def test_current_process_and_parent_protected(instance,monkeypatch):
    monkeypatch.setattr(p,'protected_pids',lambda:{instance.process.pid})
    with pytest.raises(p.InstanceError,match='Not a verified'):
        p.validate(instance.process,instance.data)


def test_multiple_instances_require_selection(instance,monkeypatch):
    monkeypatch.setattr(p,'discover',lambda data:[instance,instance] if data is None else [instance])
    with pytest.raises(p.InstanceError,match='Multiple BlackBox'):
        p.select_instance()
    assert p.select_instance(instance.data)==instance


def test_no_process(monkeypatch):
    monkeypatch.setattr(p,'discover',lambda data:[])
    assert p.select_instance() is None


def test_port_conflict_is_actionable():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        sock.listen()
        with pytest.raises(p.InstanceError,match='still busy'):
            p.wait_port(sock.getsockname()[1],timeout=0)


def test_asset_warning_requires_explicit_build(tmp_path):
    from types import SimpleNamespace
    settings=SimpleNamespace(frontend_dir=tmp_path/'dist')
    assert 'build missing' in p.asset_warnings(settings)[0]
    settings.frontend_dir.mkdir()
    (settings.frontend_dir/'index.html').write_text('built')
    assert p.asset_warnings(settings)==[]
    (tmp_path/'src').mkdir()
    (tmp_path/'src'/'app.tsx').write_text('new')
    os.utime(tmp_path/'src'/'app.tsx',(2000000000,2000000000))
    assert 'newer' in p.asset_warnings(settings)[0]


@pytest.mark.parametrize('mode',['error','timeout','invalid_pid'])
def test_failed_launch_terminates_its_child(instance,monkeypatch,mode):
    calls=[]
    class Child:
        pid=99999999
        def poll(self):
            return None
        def terminate(self):
            calls.append('term')
        def wait(self,timeout):
            calls.append('wait')
            return 0
        def kill(self):
            calls.append('kill')
    def spawn(argv,**kwargs):
        if mode=='error':
            os.write(kwargs['pass_fds'][0],b'{"error":"Missing fixture dependency"}\n')
        elif mode=='invalid_pid':
            os.write(kwargs['pass_fds'][0],b'{"pid":123}\n')
        return Child()
    monkeypatch.setattr(p,'wait_port',lambda *args:None)
    monkeypatch.setattr(p.subprocess,'Popen',spawn)
    with pytest.raises(p.InstanceError):
        p.launch(instance.config,timeout=0 if mode=='timeout' else 1)
    assert calls==['term','wait']


def test_live_lock_owner_is_required_even_with_correct_record(instance,monkeypatch):
    monkeypatch.setattr(p,'lock_owned',lambda path,pid:False)
    with pytest.raises(p.InstanceError,match='active server lock'):
        p.validate(instance.process,instance.data)


def test_foreign_directory_is_rejected(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(p.InstanceError,match='private data directory'):
        p.private_directory(tmp_path)


def test_live_record_with_missing_lock_is_not_deleted(instance):
    with pytest.raises(p.InstanceError,match='still alive'):
        p.clean_stale(instance.data)
    assert p.record_path(instance.data).exists()


@pytest.mark.parametrize('pid',[None,'123',True,-1])
def test_malformed_pid_is_actionable_not_traceback(tmp_path,pid):
    tmp_path.chmod(0o700)
    p.atomic_write(tmp_path/'instance.json',json.dumps({'pid':pid}).encode())
    with pytest.raises(p.InstanceError,match='Invalid instance record'):
        p.clean_stale(tmp_path)


def test_only_backend_owned_fixed_tools_are_cleaned_up():
    import shutil
    import subprocess
    if not shutil.which('cat') or not shutil.which('sleep'):
        pytest.skip('Linux cat/sleep fixture unavailable')
    owned=subprocess.Popen([shutil.which('cat')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL)
    unrelated=subprocess.Popen([shutil.which('sleep'),'30'])
    handles=[]
    try:
        handles=p.tool_handles(p.inspect(os.getpid()))
        assert owned.pid in [pid for pid,fd in handles]
        assert unrelated.pid not in [pid for pid,fd in handles]
        p.stop_tools([(pid,fd) for pid,fd in handles if pid==owned.pid],lambda message:None)
        owned.wait(timeout=3)
        assert unrelated.poll() is None
    finally:
        for _,fd in handles:
            os.close(fd)
        for child in [owned,unrelated]:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
        owned.stdin.close()


def test_restart_controller_without_record_is_not_a_backend(instance):
    from dataclasses import replace
    p.record_path(instance.data).unlink()
    process=replace(instance.process,argv=(sys.executable,'-m','blackbox','restart'))
    with pytest.raises(p.InstanceError,match='Restart controller'):
        p.validate(process,instance.data)


def test_reused_foreign_uid_pid_can_be_cleaned_without_signalling(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    p.atomic_write(tmp_path/'instance.json',json.dumps({'pid':99999999,'source':str(p.PACKAGE),'data_dir':str(tmp_path)}).encode())
    def foreign(pid):
        raise p.InstanceError('Process belongs to a different user')
    monkeypatch.setattr(p,'inspect',foreign)
    p.clean_stale(tmp_path)
    assert not (tmp_path/'instance.json').exists()


def test_another_app_record_is_not_removed_even_when_pid_stale(tmp_path):
    tmp_path.chmod(0o700)
    record={'pid':99999999,'source':'some-other-application','data_dir':str(tmp_path)}
    p.atomic_write(tmp_path/'instance.json',json.dumps(record).encode())
    with pytest.raises(p.InstanceError,match='another application'):
        p.clean_stale(tmp_path)
    assert json.loads((tmp_path/'instance.json').read_text())==record


def test_descriptor_lock_proof_does_not_depend_on_global_proc_locks(tmp_path,monkeypatch):
    path=tmp_path/'server.lock'
    original=Path.read_text
    def restricted(path,*args,**kwargs):
        if str(path)=='/proc/locks':
            raise PermissionError('hidden global lock table')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',restricted)
    with p.file_lock(path):
        assert p.lock_owned(path,os.getpid())
        assert os.getpid() in [owner.pid for owner in p.lock_holders(path)]


def test_inherited_flock_descriptor_identifies_current_holder(tmp_path):
    import subprocess
    path=tmp_path/'server.lock'
    child=None
    try:
        with p.file_lock(path) as fd:
            child=subprocess.Popen([sys.executable,'-c',"import time;print('ready',flush=True);time.sleep(20)"],pass_fds=(fd,),stdout=subprocess.PIPE,text=True)
            assert child.stdout.readline().strip()=='ready'
        assert p.lock_owned(path,child.pid)
    finally:
        if child:
            child.terminate()
            child.wait(timeout=3)
            child.stdout.close()


def test_lifecycle_lock_contention_has_bounded_owner_diagnostic(tmp_path):
    import time
    path=tmp_path/'lifecycle.lock'
    with p.file_lock(path):
        started=time.monotonic()
        with pytest.raises(p.InstanceError,match='lifecycle-operation'):
            with p.file_lock(path,timeout=.05):
                pass
        assert .04<=time.monotonic()-started<2
    assert path.exists()


def test_recovered_registration_still_checks_pid_identity(instance,monkeypatch):
    from dataclasses import replace
    record=json.loads(p.record_path(instance.data).read_text())
    record.update(pid=88888888,start='obsolete')
    p.atomic_write(p.record_path(instance.data),json.dumps(record).encode())
    monkeypatch.setattr(p,'legacy_config',lambda process,data:instance.config)
    recovered=p.validate(instance.process,instance.data,allow_stale=True)
    assert recovered.recovered_record
    signals=mock_stop(monkeypatch,recovered,[False])
    monkeypatch.setattr(p,'inspect',lambda pid:replace(instance.process,start='new-process'))
    with pytest.raises(p.InstanceError,match='identity changed'):
        p.stop(recovered)
    assert signals==[]


def test_independent_legacy_operation_blocks_shutdown_without_unlink(tmp_path):
    path=tmp_path/'control.lock'
    with p.file_lock(path):
        inode=path.stat().st_ino
        with pytest.raises(p.InstanceError,match='legacy lifecycle operation is still active; no signal was sent'):
            p.wait_legacy_operation(tmp_path,timeout=.01)
        assert p.lock_owned(path,os.getpid())
        assert path.stat().st_ino==inode
    p.wait_legacy_operation(tmp_path,timeout=.01)


def test_backend_legacy_lock_owner_is_described_as_backend(instance,monkeypatch):
    monkeypatch.setattr(p,'lock_holders',lambda path:[instance.process])
    message=p.occupied_lock(instance.data/'control.lock')
    assert f'backend PID {instance.process.pid}' in message
    assert 'lifecycle-operation lock' in message
