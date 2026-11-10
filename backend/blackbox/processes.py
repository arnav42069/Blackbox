"""Linux instance lifecycle. Never trust a PID file or a command substring alone."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import time

from .storage.files import atomic_write

PACKAGE = Path(__file__).resolve().parent


class InstanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    start: str
    executable: str
    argv: tuple[str, ...]
    ppid: int


def inspect(pid):
    folder = Path('/proc')/str(pid)
    if folder.stat().st_uid != os.getuid():
        raise InstanceError('Process belongs to a different user')
    raw = (folder/'stat').read_text()
    fields = raw[raw.rfind(')')+2:].split()
    if fields[0] == 'Z':
        raise ProcessLookupError(pid)
    return Process(pid, fields[19], os.readlink(folder/'exe'),
                   tuple(v.decode() for v in (folder/'cmdline').read_bytes().split(b'\0') if v), int(fields[1]))


def protected_pids():
    result = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        try:
            pid = inspect(pid).ppid
        except (OSError, InstanceError, ValueError):
            break
    return result | {1}


def same_interpreter(argument):
    # Python/python3 aliases in the same installed environment are equivalent;
    # a different venv is not evidence for this installation.
    path=Path(os.path.abspath(argument))
    current=Path(sys.executable).absolute()
    return path.parent==current.parent and path.resolve()==current.resolve()


def backend_command(argv, *, allow_restart=True):
    """Only the installed entrypoint or exact module launch, never uvicorn matches."""
    executable = Path(sys.executable).absolute()
    entry = executable.parent/'blackbox'
    if len(argv) >= 2 and Path(argv[1]).resolve() == entry.resolve():
        options = argv[2:]
    elif len(argv) >= 3 and same_interpreter(argv[0]) and argv[1:3] in {
            ('-m', 'blackbox'), ('-m', 'blackbox.cli')}:
        options = argv[3:]
    else:
        return False
    # Lifecycle/doctor processes must never be mistaken for backends.
    i=0
    restart_seen=False
    while i<len(options):
        token=options[i]
        if token=='restart' and not restart_seen and allow_restart:
            # With no prior instance, restart becomes a normal foreground backend.
            restart_seen=True
            i+=1
        elif token in {'--no-browser','--json'}:
            i+=1
        elif token in {'--port','--model','--data-dir','--config-dir'}:
            if i+1>=len(options):
                return False
            i+=2
        elif any(token.startswith(name+'=') for name in ('--port','--model','--data-dir','--config-dir')):
            i+=1
        else:
            return False
    return True


def private_directory(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode):
        raise InstanceError(f'{path}: expected a private data directory owned by this user (0700)')
    return path


def _matches_lock(line, info, pid=None):
    parts=line.split()
    if parts and parts[0]=='lock:':
        parts=parts[1:]
    if len(parts)<6 or parts[1:4]!=['FLOCK','ADVISORY','WRITE'] or (pid is not None and parts[4]!=str(pid)):
        return False
    try:
        major,minor,inode=parts[5].split(':')
        return (int(major,16),int(minor,16),int(inode))==(os.major(info.st_dev),os.minor(info.st_dev),info.st_ino)
    except ValueError:
        return False


def lock_owned(path,pid):
    """Prove ownership via the process's actual locked descriptor.

    /proc/locks alone can omit locks across PID namespaces; fdinfo also accounts
    for an inherited flock descriptor without trusting its original owner PID.
    An unrelated process merely opening the file has no matching fdinfo lock.
    """
    try:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            return False
        folder=Path('/proc')/str(pid)
        for descriptor in (folder/'fd').iterdir():
            try:
                opened=descriptor.stat()
                if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
                    continue
                details=(folder/'fdinfo'/descriptor.name).read_text()
                if any(_matches_lock(line,info) for line in details.splitlines()):
                    return True
            except FileNotFoundError:
                continue
    except (FileNotFoundError,ProcessLookupError,PermissionError):
        return False
    return False


def lock_holders(path):
    holders=[]
    for folder in Path('/proc').iterdir():
        if not folder.name.isdecimal():
            continue
        try:
            process=inspect(int(folder.name))
            if lock_owned(path,process.pid):
                holders.append(process)
        except (OSError,InstanceError,ValueError):
            continue
    return holders


def occupied_lock(path):
    holders=lock_holders(path)
    descriptions=[]
    for process in holders:
        operation=path.name in {'control.lock','lifecycle.lock'}
        backend=backend_command(process.argv) and lock_owned(path.parent/'server.lock',process.pid)
        if backend:
            kind='backend'
        elif operation and (backend_command(process.argv) or any(v in {'kill','restart'} for v in process.argv[1:])):
            kind='lifecycle/startup operation'
        else:
            kind='unverified process'
        descriptions.append(f'{kind} PID {process.pid} ({Path(process.executable).name})')
    owner=', '.join(descriptions) or 'an owner not visible through /proc'
    role='lifecycle-operation' if path.name in {'control.lock','lifecycle.lock'} else 'backend-instance'
    return f'{owner} holds {role} lock {path}. No locked file was removed.'


@contextmanager
def file_lock(path, *, timeout=0):
    path=Path(path)
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InstanceError(f'Unsafe lock file: {path}')
        deadline=time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic()>=deadline:
                    raise InstanceError(occupied_lock(path)+' Retry after the active operation finishes.') from error
                time.sleep(min(.05,max(0,deadline-time.monotonic())))
        yield fd
    finally:
        os.close(fd)


def wait_legacy_operation(data, instance=None, timeout=5):
    """A pre-upgrade backend may retain control.lock while starting.

    Never wait on that backend to relinquish its own lock before shutdown. An
    independent old-version lifecycle controller, however, must finish first.
    New controllers serialize on lifecycle.lock, which old backends cannot hold.
    """
    path=Path(data)/'control.lock'
    if not path.exists():
        return
    deadline=time.monotonic()+timeout
    while True:
        holders=lock_holders(path)
        if holders and instance and all(p.pid==instance.process.pid for p in holders):
            return
        if not holders:
            with file_lock(path):
                return
        if time.monotonic()>=deadline:
            raise InstanceError(occupied_lock(path)+' A legacy lifecycle operation is still active; no signal was sent.')
        time.sleep(.05)


def record_path(data):
    return Path(data)/'instance.json'


def read_record(data):
    path = record_path(data)
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstanceError(f'Unsafe instance record: {path}')
    try:
        if info.st_size > 16384:
            raise ValueError('oversized')
        value = json.loads(path.read_text())
        if not isinstance(value,dict):
            raise ValueError('not an object')
        if 'pid' in value and (type(value['pid']) is not int or value['pid']<=1):
            raise ValueError('invalid PID')
        return value
    except ValueError as e:
        raise InstanceError(f'Invalid instance record {path}; refusing to guess process identity') from e


@dataclass
class Instance:
    process: Process
    data: Path
    config: dict
    recovered_record: bool = False


def legacy_config(process, data):
    """Recover supported arguments/environment from a pre-record CLI backend."""
    env = dict(item.decode().split('=',1) for item in (Path('/proc')/str(process.pid)/'environ').read_bytes().split(b'\0') if b'=' in item)
    argv = process.argv
    def option(name, default):
        for i, arg in enumerate(argv):
            if arg == name and i+1 < len(argv):
                return argv[i+1]
            if arg.startswith(name+'='):
                return arg.split('=',1)[1]
        return default
    from .settings import Settings
    settings = Settings(data_dir=data, config_dir=option('--config-dir', env.get('BLACKBOX_CONFIG_DIR', str(Path.home()/'.config/blackbox'))),
                        ollama_url=env.get('BLACKBOX_OLLAMA_URL','http://127.0.0.1:11434'))
    # Read preferences without calling prepare() or modifying the running instance.
    import yaml
    from .settings import Preferences
    prefs = settings.config_dir/'config.yaml'
    model = option('--model',None)
    if model is None:
        model = Preferences.model_validate(yaml.safe_load(prefs.read_text()) or {}).model if prefs.exists() else settings.prefs.model
    return {'model':option('--model',model), 'port':int(option('--port','8765')), 'data_dir':str(data),
            'config_dir':str(settings.config_dir.absolute()), 'ollama_url':settings.ollama_url, 'no_browser':'--no-browser' in argv}


def expired_record(record):
    try:
        previous=inspect(record['pid'])
        return previous.start!=record.get('start')
    except (ProcessLookupError,FileNotFoundError,InstanceError):
        return True
    except PermissionError:
        return False


def validate(process, data, *, allow_stale=False):
    if process.pid in protected_pids() or not backend_command(process.argv):
        raise InstanceError('Not a verified BlackBox backend command')
    if process.executable != os.readlink('/proc/self/exe'):
        raise InstanceError('Backend executable does not match the installed Python environment')
    data = private_directory(data)
    if not lock_owned(data/'server.lock',process.pid):
        raise InstanceError('Backend does not own this data directory’s active server lock')
    record = read_record(data)
    recovered=False
    if record:
        expected=(process.pid,process.start,process.executable,str(data),str(PACKAGE))
        actual=(record.get('pid'),record.get('start'),record.get('executable'),record.get('data_dir'),record.get('source'))
        if actual!=expected:
            # Never signal the recorded PID on faith. Independently verify the
            # actual flock owner and executable/entrypoint, then recover config
            # only if the old registration is provably obsolete.
            if not (allow_stale and record.get('source')==str(PACKAGE) and record.get('data_dir')==str(data)
                    and backend_command(process.argv,allow_restart=False) and expired_record(record)):
                raise InstanceError('Instance record does not match the live process; possible stale PID or different installation')
            config=legacy_config(process,data)
            recovered=True
        else:
            config=record.get('config')
        if not isinstance(config,dict) or config.get('data_dir')!=str(data):
            raise InstanceError('Restart configuration points to a different data directory')
    else:
        if not backend_command(process.argv,allow_restart=False):
            raise InstanceError('Restart controller without a backend record is not an eligible server')
        config = legacy_config(process,data)
    return Instance(process,data,config,recovered)


def discover(data=None):
    """Scan /proc like pgrep, then prove exact command, executable and lock ownership."""
    result = []
    selected = Path(os.path.abspath(Path(data).expanduser())) if data is not None else None
    if selected is not None:
        record=read_record(selected) if selected.exists() else None
        if record and record.get('pid'):
            try:
                result.append(validate(inspect(record['pid']),selected,allow_stale=True))
            except (OSError,InstanceError,ValueError,KeyError):
                pass
    for folder in Path('/proc').iterdir():
        if not folder.name.isdecimal():
            continue
        try:
            process = inspect(int(folder.name))
            if not backend_command(process.argv) or process.pid in protected_pids():
                continue
            for fd in (folder/'fd').iterdir():
                try:
                    path = Path(os.readlink(fd))
                except FileNotFoundError:
                    continue
                if path.name != 'server.lock' or (selected is not None and path.parent != selected):
                    continue
                instance = validate(process,path.parent,allow_stale=True)
                if not any(i.process.pid==process.pid for i in result):
                    result.append(instance)
        except (FileNotFoundError, ProcessLookupError, PermissionError, InstanceError, ValueError, KeyError):
            continue
    return result


def select_instance(data=None):
    instances = discover(data)
    if len(instances)>1:
        raise InstanceError('Multiple BlackBox instances found; choose --data-dir:\n'+ '\n'.join(f'PID {i.process.pid}: {i.data}' for i in instances))
    return instances[0] if instances else None


def check_stale_record(data):
    record=read_record(data)
    if not record:
        return
    if record.get('source')!=str(PACKAGE) or record.get('data_dir')!=str(data):
        raise InstanceError('Unrecognized instance record; refusing to remove another application’s state')
    try:
        process=inspect(int(record.get('pid',0)))
    except (ProcessLookupError,FileNotFoundError,ValueError,InstanceError):
        return
    if backend_command(process.argv) and process.start==record.get('start') and process.executable==record.get('executable'):
        raise InstanceError(f"Recorded backend PID {process.pid} is still alive but could not be verified through its lock; refusing stale cleanup or duplicate startup")


def clean_stale(data):
    """Never unlink lock files: reuse their inode only after acquiring the kernel lock."""
    if not Path(data).exists():
        return
    data = private_directory(data)
    with file_lock(data/'server.lock'):
        check_stale_record(data)
        path = record_path(data)
        if path.exists():
            read_record(data)
            path.unlink()


def exited(fd, timeout):
    return bool(select.select([fd],[],[],timeout)[0])


def signal_fd(fd, sig):
    signal.pidfd_send_signal(fd,sig)


# Only direct children created by the backend's fixed subprocess tools are eligible.
# Browser launchers and other applications are deliberately excluded.
TOOL_NAMES={'ip','ping','nmap','ffuf','ssh','curl','whatweb','openssl','uname','cat','free','lscpu','nvidia-smi','lspci'}


def tool_handles(parent):
    import shutil
    handles=[]
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        fd=None
        try:
            child=inspect(int(directory.name))
            if child.ppid!=parent.pid or child.pid in protected_pids():
                continue
            names=[name for name in TOOL_NAMES if shutil.which(name) and
                   (child.executable==str(Path(shutil.which(name)).resolve()) or
                    (len(child.argv)>1 and child.argv[1]==shutil.which(name)))]
            if not names:
                continue
            fd=os.pidfd_open(child.pid)
            if inspect(child.pid)!=child:
                os.close(fd)
                continue
            handles.append((child.pid,fd))
        except (OSError,InstanceError,ValueError):
            if fd is not None:
                os.close(fd)
    return handles


def stop_tools(handles,emit):
    for pid,fd in handles:
        if exited(fd,0):
            continue
        try:
            signal_fd(fd,signal.SIGTERM)
            if not exited(fd,.5):
                signal_fd(fd,signal.SIGKILL)
                if not exited(fd,1):
                    raise InstanceError(f'Backend stopped but owned tool PID {pid} did not exit')
            emit(f'Stopped backend-owned tool PID {pid}.')
        except ProcessLookupError:
            pass


def stop(instance, emit=print):
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise InstanceError('Safe process control requires Linux pidfd support (Linux 5.3+ and Python with pidfd APIs)')
    try:
        fd = os.pidfd_open(instance.process.pid)
    except PermissionError as e:
        raise InstanceError(f'Permission denied inspecting PID {instance.process.pid}; use the backend owner') from e
    except ProcessLookupError:
        emit('BlackBox already stopped.')
        return 'already stopped'
    children=[]
    try:
        if exited(fd,0):
            emit('BlackBox already stopped.')
            return 'already stopped'
        # Recheck after pinning a kernel process handle: PID reuse cannot redirect signals.
        current = inspect(instance.process.pid)
        if current != instance.process:
            raise InstanceError('Process identity changed; refusing to signal a reused PID')
        validate(current,instance.data,allow_stale=instance.recovered_record)
        children=tool_handles(current)
        emit(f'Found BlackBox backend (PID {current.pid})')
        emit('Sending SIGTERM...')
        signal_fd(fd,signal.SIGTERM)
        used = 'SIGTERM'
        if not exited(fd,3):
            children.extend(tool_handles(current))
            emit('Process still running. Sending SIGKILL...')
            signal_fd(fd,signal.SIGKILL)
            used = 'SIGKILL'
            if not exited(fd,3):
                raise InstanceError(f'PID {current.pid} did not exit after SIGKILL; inspect OS process state')
        stop_tools(children,emit)
        emit(f'BlackBox stopped successfully. PID {current.pid}; {used} required.')
        return used
    except ProcessLookupError:
        return 'already stopped'
    except PermissionError as e:
        raise InstanceError(f'Permission denied stopping PID {instance.process.pid}; run as the backend owner, not another user') from e
    finally:
        for _,child_fd in children:
            os.close(child_fd)
        os.close(fd)


def port_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try:
            sock.bind(('127.0.0.1',port))
            return True
        except OSError:
            return False


def wait_port(port, timeout=3):
    deadline = time.monotonic()+timeout
    while not port_free(port):
        if time.monotonic()>=deadline:
            raise InstanceError(f'Port {port} is still busy; another service may own it. Choose --port; no unrelated process was terminated.')
        time.sleep(.05)


def launch(config, timeout=20):
    """Fresh installed interpreter; private readiness pipe, no auth tokens on disk."""
    from .settings import Settings, Preferences
    settings = Settings(port=config['port'],data_dir=config['data_dir'],config_dir=config['config_dir'],ollama_url=config['ollama_url'])
    Preferences(model=config['model'])
    data = private_directory(settings.data_dir)
    wait_port(settings.port)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    process = None
    try:
        env = {**os.environ,'BLACKBOX_READY_FD':str(write_fd),'BLACKBOX_OLLAMA_URL':settings.ollama_url,
               'PYTHONPATH':str(PACKAGE.parent)}
        argv = [sys.executable,'-m','blackbox','--data-dir',str(data),'--config-dir',str(settings.config_dir),
                '--port',str(settings.port),'--model',config['model']]
        if config['no_browser']:
            argv.append('--no-browser')
        log_fd = os.open(data/'server.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(log_fd)
            if info.st_uid!=os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise InstanceError('server.log must be a private regular file owned by this user')
            process = subprocess.Popen(argv,env=env,cwd='/',stdin=subprocess.DEVNULL,stdout=log_fd,stderr=subprocess.STDOUT,
                                       start_new_session=True,pass_fds=(write_fd,))
        finally:
            os.close(log_fd)
        os.close(write_fd)
        write_fd = None
        deadline = time.monotonic()+timeout
        payload = b''
        while b'\n' not in payload:
            if time.monotonic()>=deadline:
                raise InstanceError(f'Backend startup timed out; check {data / "server.log"} and blackbox doctor')
            if select.select([read_fd],[],[],.1)[0]:
                chunk = os.read(read_fd,16384)
                if not chunk:
                    raise InstanceError(f'Backend exited before readiness; check {data / "server.log"} for configuration errors, missing dependencies or port conflicts')
                payload += chunk
                if len(payload)>16384:
                    raise InstanceError('Invalid backend readiness message')
        ready=json.loads(payload.split(b'\n')[0])
        if ready.get('error'):
            raise InstanceError(ready['error'])
        if ready.get('pid')!=process.pid or process.poll() is not None:
            raise InstanceError('New backend exited or returned an invalid PID')
        validate(inspect(process.pid),data)
        return ready
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def asset_warnings(settings):
    index=settings.frontend_dir/'index.html'
    if not index.is_file():
        return [f'Frontend build missing. Run npm --prefix {settings.frontend_dir.parent} run build; restart does not build assets.']
    source=settings.frontend_dir.parent/'src'
    if source.exists() and any(p.stat().st_mtime>index.stat().st_mtime for p in source.rglob('*') if p.is_file()):
        return [f'Frontend sources are newer than the installed build. Run npm --prefix {settings.frontend_dir.parent} run build.']
    return []
