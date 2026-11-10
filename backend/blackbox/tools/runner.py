from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time

from .base import ToolResult
from ..storage.files import clean_text


class SubprocessRunner:
    def __init__(self):
        self.binaries = {name: shutil.which(name) for name in ('ip', 'ping', 'nmap', 'ffuf', 'ssh', 'curl', 'whatweb', 'openssl', 'uname', 'cat', 'free', 'lscpu', 'nvidia-smi', 'lspci')}

    async def run(self, binary, args, *, timeout=30, cap=65536, on_output=None, on_command=None, config_home=None):
        executable = self.binaries.get(binary)
        if not executable:
            return ToolResult(ok=False, summary=f"Missing binary {binary}; install it and restart BlackBox")
        started = time.monotonic()
        if on_command:
            on_command([executable, *args])
        proc = await asyncio.create_subprocess_exec(executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, start_new_session=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'HOME': str(config_home or '/nonexistent'), 'XDG_CONFIG_HOME': str(config_home or '/nonexistent'), 'XDG_CONFIG_DIRS': str(config_home or '/nonexistent')})
        chunks = {'stdout': bytearray(), 'stderr': bytearray()}
        truncated = False
        total = 0

        async def collect(stream, label):
            nonlocal total, truncated
            pending = ''
            in_key = False
            def emit(line):
                nonlocal in_key
                if '-----BEGIN ' in line and 'PRIVATE KEY-----' in line:
                    in_key = True
                    if on_output:
                        on_output(label, '[REDACTED PRIVATE KEY]\n')
                if in_key:
                    if '-----END ' in line and 'PRIVATE KEY-----' in line:
                        in_key = False
                    return
                if on_output:
                    on_output(label, clean_text(line))
            while data := await stream.read(4096):
                room = max(0, cap - total)
                take = data[:room]
                total += len(take)
                chunks[label].extend(take)
                pending += take.decode(errors='replace')
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    emit(line + '\n')
                if len(data) > room:
                    truncated = True
                    stop_group(signal.SIGKILL)
                    if pending:
                        emit(pending)
                    return
            if pending:
                emit(pending)

        def stop_group(sig):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass

        async def terminate():
            stop_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            # Also kill descendants when the parent already exited.
            stop_group(signal.SIGKILL)
            await proc.wait()

        readers = [asyncio.create_task(collect(proc.stdout, 'stdout')), asyncio.create_task(collect(proc.stderr, 'stderr'))]
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(proc.wait(), *readers)
        except asyncio.TimeoutError:
            timed_out = True
            await terminate()
        except BaseException:
            await terminate()
            raise
        finally:
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            stop_group(signal.SIGKILL)
        return ToolResult(ok=proc.returncode == 0 and not timed_out and not truncated, exit_code=proc.returncode, stdout=clean_text(chunks['stdout'].decode(errors='replace')), stderr=clean_text(chunks['stderr'].decode(errors='replace')), summary='Tool timed out' if timed_out else 'Output limit reached' if truncated else f'{binary} exited {proc.returncode}', elapsed_ms=int((time.monotonic()-started)*1000), truncated=truncated)
