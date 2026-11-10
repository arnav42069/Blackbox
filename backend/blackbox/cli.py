import argparse
import asyncio
import json
import socket
import webbrowser



def main():
    from pathlib import Path
    import os
    from .processes import InstanceError
    parser = argparse.ArgumentParser(description="BlackBox — scoped local Ollama workbench")
    parser.add_argument("command", nargs="?", choices=["doctor", "kill", "restart"],
                        help="doctor: diagnostics; kill: safely stop backend; restart: preserve configuration and start fresh")
    parser.add_argument("--no-browser", action="store_true", default=None)
    parser.add_argument("--port", type=int, help="Listening port (default 8765; restart preserves existing port)")
    parser.add_argument("--model", help="Ollama model; overrides preserved model on restart")
    parser.add_argument("--data-dir", type=Path, help="Data directory; selects an instance when multiple backends run")
    parser.add_argument("--config-dir", type=Path, help="Configuration directory")
    parser.add_argument("--json", action="store_true", help="Machine-readable doctor output")
    parser.add_argument("--probe", action="store_true", help="With doctor: test selected model vision/tools and cache by digest")
    args = parser.parse_args()
    if args.probe and args.command != "doctor":
        parser.error("--probe requires doctor")
    try:
        from .settings import Settings
        values = {'port':args.port if args.port is not None else 8765}
        if args.data_dir is not None:
            values['data_dir']=args.data_dir.expanduser().absolute()
        if args.config_dir is not None:
            values['config_dir']=args.config_dir.expanduser().absolute()
        settings = Settings(**values)
        if args.model is not None:
            from .settings import Preferences
            Preferences(model=args.model)
        if args.command in {'kill','restart'}:
            selector = args.data_dir or os.environ.get('BLACKBOX_DATA_DIR')
            lifecycle(args, settings, selector)
        else:
            settings.prepare()
            if args.model is not None:
                settings.prefs.model = args.model
            if args.command == 'doctor':
                asyncio.run(doctor(settings,args.json,args.probe))
            else:
                serve(settings, args)
    except ImportError as e:
        parser.exit(1, f'BlackBox: Missing Python dependency {e.name}; refresh this installation’s Python environment, then run blackbox doctor.\n')
    except (InstanceError, OSError, ValueError) as e:
        parser.exit(1, f'BlackBox: {e}\n')


def lifecycle(args, settings, selector):
    import os
    from .processes import select_instance, private_directory, file_lock, clean_stale, stop, wait_port, launch
    print('BlackBox: Searching for running instances...' if args.command=='kill' else 'BlackBox: Restarting...',flush=True)
    instance=select_instance(selector)
    data=instance.data if instance else settings.data_dir.expanduser().absolute()
    if not data.exists():
        if args.command=='kill':
            print('No running BlackBox instance found.')
            return
        settings.prepare()
    data=private_directory(data)
    with file_lock(data/'lifecycle.lock',timeout=5):
        instance=select_instance(data)
        from .processes import wait_legacy_operation
        wait_legacy_operation(data,instance)
        if instance:
            config=dict(instance.config)
            if args.command=='restart':
                print(f"Preserving model: {config['model']}\nPreserving port: {config['port']}",flush=True)
            if args.command=='restart':
                import uvicorn
                from .app import create_app  # Validate installed runtime imports before stopping.
                if args.port is not None and args.port!=config['port']:
                    wait_port(args.port, timeout=0)
            stop(instance)
            wait_port(config['port'])
            clean_stale(data)
            if args.command=='kill':
                return
            if args.model is not None:
                config['model']=args.model
            if args.port is not None:
                config['port']=args.port
            if args.config_dir is not None:
                config['config_dir']=str(args.config_dir.expanduser().absolute())
            if args.no_browser is not None:
                config['no_browser']=args.no_browser
            print('Starting BlackBox...',flush=True)
            ready=launch(config)
            print(f"New backend PID: {ready['pid']}\nModel: {ready['model']}\nListening on: {ready['address']}",flush=True)
            for message in ready.get('warnings',[]):
                print(message,flush=True)
            print(f"BlackBox restarted successfully.\nPairing URL: {ready['pairing_url']}",flush=True)
            return
        from .processes import lock_holders, occupied_lock, InstanceError
        if lock_holders(data/'server.lock'):
            raise InstanceError(occupied_lock(data/'server.lock')+' Identity verification failed; no signal was sent.')
        clean_stale(data)
        if args.command=='kill':
            print('No running BlackBox instance found.')
            return
    # With no old instance, restart has normal foreground startup semantics.
    settings.prepare()
    if args.model is not None:
        settings.prefs.model=args.model
    serve(settings,args)


def serve(settings,args):
    import os
    import threading
    from contextlib import ExitStack
    import uvicorn
    from .processes import file_lock, private_directory, inspect, record_path, PACKAGE, asset_warnings
    from .storage.files import atomic_write
    from .storage.db import uid
    from .app import create_app
    ready_value=os.environ.pop('BLACKBOX_READY_FD',None)
    ready_fd=int(ready_value) if ready_value is not None else None
    data=private_directory(settings.data_dir)
    control=None
    record=None
    opener=None
    def report(payload):
        nonlocal ready_fd
        if ready_fd is not None:
            try:
                os.write(ready_fd,(json.dumps(payload)+'\n').encode())
            finally:
                os.close(ready_fd)
                ready_fd=None
    try:
        with ExitStack() as locks:
            if ready_value is None:
                control=locks.enter_context(file_lock(data/'lifecycle.lock',timeout=5))
            from .processes import wait_legacy_operation, select_instance
            wait_legacy_operation(data,select_instance(data))
            locks.enter_context(file_lock(data/'server.lock'))
            from .processes import port_free, InstanceError
            if not port_free(settings.port):
                raise InstanceError(f'Port {settings.port} is busy; choose --port. No unrelated service was stopped.')
            from .processes import check_stale_record
            check_stale_record(data)
            if args.model is not None:
                settings.prefs.model=args.model
            process=inspect(os.getpid())
            config={'model':settings.prefs.model,'port':settings.port,'data_dir':str(data),
                    'config_dir':str(settings.config_dir.expanduser().absolute()),'ollama_url':settings.ollama_url,
                    'no_browser':bool(args.no_browser)}
            record={'id':uid(),'pid':process.pid,'start':process.start,'executable':process.executable,
                    'data_dir':str(data),'source':str(PACKAGE),'config':config}
            atomic_write(record_path(data),json.dumps(record).encode())
            # A backend holds server.lock for its lifetime, not the operation lock.
            # Once registered, even slow app startup must remain stoppable.
            if control is not None:
                import fcntl
                fcntl.flock(control,fcntl.LOCK_UN)
                control=None
            app=create_app(settings)
            if args.model is not None:
                settings.prefs.model=args.model
            address=f'http://127.0.0.1:{settings.port}'
            pairing=address+'/#pair='+app.state.auth.pairing
            warnings=app.state.dependencies['messages']+asset_warnings(settings)
            class Server(uvicorn.Server):
                async def startup(self,sockets=None):
                    nonlocal opener,control
                    await super().startup(sockets)
                    if not self.started:
                        return
                    # Publish readiness only after uvicorn actually owns the listening socket.
                    if control is not None:
                        import fcntl
                        fcntl.flock(control,fcntl.LOCK_UN)
                        control=None
                    if ready_value is not None:
                        report({'pid':process.pid,'model':settings.prefs.model,'address':address,
                                'pairing_url':pairing,'warnings':warnings})
                    else:
                        for message in warnings:
                            print(message,flush=True)
                        print(f'BlackBox: {address} (loopback only)\nNew backend PID: {process.pid}\nModel: {settings.prefs.model}',flush=True)
                        print('Pairing link (one use, expires in 120 seconds; keep private):\n'+pairing,flush=True)
                    if not args.no_browser:
                        opener=threading.Timer(.2,webbrowser.open,args=(pairing,))
                        opener.daemon=True
                        opener.start()
            Server(uvicorn.Config(app,host='127.0.0.1',port=settings.port,log_level='warning',access_log=False)).run()
    except BaseException as e:
        report({'error':str(e) or type(e).__name__})
        raise
    finally:
        if opener:
            opener.cancel()
        if ready_fd is not None:
            os.close(ready_fd)
        # The server lock is never deleted; the record is removed only if still ours.
        if record:
            try:
                with file_lock(data/'server.lock'):
                    saved=json.loads(record_path(data).read_text())
                    if saved.get('id')==record['id']:
                        record_path(data).unlink()
            except (FileNotFoundError, InstanceError, ValueError):
                pass


async def doctor(settings, machine=False, probe=False):
    import os
    import shutil
    from .core.ollama_client import OllamaClient, OllamaError
    from .storage.db import Database
    db = Database(settings.data_dir / "blackbox.db", recover=False)
    client = OllamaClient(settings, db)
    from .dependencies import detect
    dependencies = detect()
    result = {"dependencies": dependencies, "binaries": {n: shutil.which(n) for n in ["ping", "nmap", "ffuf", "ssh", "curl", "whatweb", "grim", "wl-paste"]}, "storage_private": oct(settings.data_dir.stat().st_mode & 0o777), "ssh_agent": bool(os.environ.get("SSH_AUTH_SOCK"))}
    try:
        if probe:
            result["probes"] = await client.probe(settings.prefs.model)
        result["models"] = await client.models()
        result["ollama"] = "connected"
    except OllamaError as e:
        result["ollama"] = str(e)
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", settings.port))
            result["port"] = "available"
        except OSError:
            result["port"] = "busy; choose --port"
    print(json.dumps(result, indent=2))
    if not machine:
        print("Missing tools: Arch packages iputils nmap openssh; ffuf is preferred; whatweb/grim/wl-clipboard are optional.\nNo models? Run: ollama serve; ollama pull qwen3.5:4b")
    await client.close()
    db.close()


if __name__ == "__main__":
    main()
