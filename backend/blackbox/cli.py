import argparse
import asyncio
import json
import socket
import webbrowser

from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="BlackBox — scoped local Ollama workbench")
    parser.add_argument("command", nargs="?", choices=["doctor"], help="Check local dependencies and connectivity")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model")
    parser.add_argument("--json", action="store_true", help="Machine-readable doctor output")
    parser.add_argument("--probe", action="store_true", help="With doctor: test selected model vision/tools and cache by digest")
    args = parser.parse_args()
    if args.probe and args.command != "doctor":
        parser.error("--probe requires doctor")
    settings = Settings(port=args.port)
    settings.prepare()
    if args.model:
        settings.prefs.model = args.model
    if args.command == "doctor":
        asyncio.run(doctor(settings, args.json, args.probe))
        return
    import os
    import fcntl
    import uvicorn
    lock_path = settings.data_dir / 'server.lock'
    lock = lock_path.open('a')
    lock_path.chmod(0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('BlackBox is already running with this data directory')
    with socket.socket() as check:
        try:
            check.bind(('127.0.0.1', settings.port))
        except OSError:
            parser.error(f'Port {settings.port} is busy; choose --port')
    from .app import create_app
    app = create_app(settings)
    if args.model:
        settings.prefs.model = args.model
    url = f"http://127.0.0.1:{args.port}/#pair={app.state.auth.pairing}"
    print(f"BlackBox: http://127.0.0.1:{args.port} (loopback only)", flush=True)
    print("Pairing link (one use, expires in 120 seconds; keep private):\n" + url, flush=True)
    if not args.no_browser:
        import threading
        opener = threading.Timer(0.6, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


async def doctor(settings, machine=False, probe=False):
    import os
    import shutil
    from .core.ollama_client import OllamaClient, OllamaError
    from .storage.db import Database
    db = Database(settings.data_dir / "blackbox.db", recover=False)
    client = OllamaClient(settings, db)
    result = {"binaries": {n: shutil.which(n) for n in ["ping", "nmap", "ffuf", "ssh", "curl", "whatweb", "grim", "wl-paste"]}, "storage_private": oct(settings.data_dir.stat().st_mode & 0o777), "ssh_agent": bool(os.environ.get("SSH_AUTH_SOCK"))}
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
        print("Missing tools: Arch packages iputils nmap openssh; ffuf/whatweb/grim/wl-clipboard are optional.\nNo models? Run: ollama serve; ollama pull qwen3.5:4b")
    await client.close()
    db.close()


if __name__ == "__main__":
    main()
