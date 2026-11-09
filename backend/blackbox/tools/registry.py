from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass

from pydantic import Field

from . import network, workspace, web, ssh, host
from .base import Arguments, ToolResult, ToolSpec
from .runner import SubprocessRunner
from ..core.policy import PolicyEngine
from ..core.scope import Scope, ScopeDenied
from ..storage.db import now, uid
from ..storage.files import atomic_write, clean_text, clean_value


class Artifact(Arguments):
    filename: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\.(txt|md|json)$')
    text: str = Field(max_length=32000)


async def save_artifact(ctx, args):
    folder = ctx.settings.data_dir/'artifacts'/ctx.sid
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder/args.filename
    if path.exists():
        raise ValueError('Artifact exists; choose a new filename')
    atomic_write(path, clean_text(args.text).encode())
    return ToolResult(ok=True, summary='Session artifact saved', evidence=[{'filename': args.filename}])


@dataclass
class ToolContext:
    settings: object
    db: object
    sid: str
    rid: str
    scope: Scope
    runner: SubprocessRunner
    agent: object
    calls: int = 0
    requests: int = 0
    tool_id: str = ''

    def output(self, stream, chunk):
        self.agent.event(self.sid, self.rid, 'tool_output', tool_id=self.tool_id, stream=stream, chunk=clean_text(chunk))

    def command(self, argv):
        self.agent.event(self.sid, self.rid, 'tool_command', tool_id=self.tool_id, argv=clean_value(argv))

    def request(self):
        if self.requests >= self.scope.data.get('max_requests_per_run', 0):
            raise ScopeDenied('HTTP request budget exhausted')
        self.requests += 1


class Registry:
    def __init__(self):
        self.runner = SubprocessRunner()
        self.policy = PolicyEngine()
        self.target_lock = asyncio.Lock()
        self.specs = {}
        self.add('host.preflight', 'Measure actual distribution, kernel, RAM, CPU, GPU and installed tool versions with audited fixed host commands. Unknown means not measured.', network.Empty, host.preflight, timeout=45)
        self.add('host.diagnostics', 'Run fixed read-only host checks for selected sections: system, memory, cpu, gpu, tools. No raw command arguments.', host.Diagnostics, host.diagnostics, timeout=45)
        self.add('workspace.list', 'List safe files in the application workspace.', workspace.LocalPath, workspace.list_dir)
        self.add('workspace.read', 'Read bounded text in the workspace; no credentials.', workspace.Read, workspace.read)
        self.add('workspace.search', 'Search workspace text using a literal substring.', workspace.Search, workspace.search)
        self.add('net.ping', 'Send two ICMP packets to one authorized host.', network.Target, network.ping, target=True, timeout=10, binary='ping')
        self.add('net.scan_ports', 'Bounded TCP connect scan on one authorized host. Requires active scans.', network.Scan, network.scan, risk='active_scan', target=True, timeout=40, binary='nmap')
        self.add('web.inspect', 'Read public HTTP(S), pinning all destinations and redirects to authorized scope.', web.Inspect, web.inspect, target=True)
        self.add('web.discover_paths', 'Check a curated small list of public web paths. Requires active scans.', web.Discover, web.discover, risk='active_scan', target=True, timeout=60, binary='ffuf')
        self.add('ssh.probe', 'Verify existing authorized public-key SSH/SFTP access using a configured profile.', ssh.SSHArgs, ssh.probe, target=True)
        self.add('ssh.list_dir', 'Read-only SFTP listing inside approved lab paths.', ssh.RemotePath, ssh.list_dir, target=True)
        self.add('ssh.find_files', 'Find candidate flag filenames inside approved paths, with depth and entry caps.', ssh.Find, ssh.find_files, target=True)
        self.add('ssh.read_file', 'Read bounded candidate file via SFTP; secret stores forbidden.', ssh.RemoteRead, ssh.read_file, target=True)
        self.add('artifacts.save', 'Save generated text to a new file in this session artifacts folder.', Artifact, save_artifact)

    def add(self, name, description, schema, run, risk='read_only', target=False, timeout=25, binary=None):
        self.specs[name] = ToolSpec(name, description, schema, risk, target, timeout, run, binary)

    def schemas(self):
        return [s.schema() for s in self.specs.values() if not s.binary or shutil.which(s.binary)]

    async def execute(self, ctx, name, raw):
        ctx.calls += 1
        tid = ctx.tool_id = uid()
        started = now()
        args = json.loads(json.dumps(raw))
        # Redact user-controlled data before audit logging, including generated report text.
        safe_args = clean_value(args)
        ctx.db.execute('INSERT INTO tool_events VALUES (?,?,?,?,?,NULL,?,?,NULL)', (tid, ctx.rid, name, json.dumps(safe_args), '{}', 'running', started))
        self_event = lambda kind, **payload: ctx.agent.event(ctx.sid, ctx.rid, kind, tool_id=tid, name=name, **payload)
        result = ToolResult(ok=False)
        outcome = 'failed'
        try:
            spec = self.specs.get(name)
            if not spec:
                raise ScopeDenied('Unregistered tool; arbitrary commands are unavailable')
            arguments = spec.argument_schema.model_validate(args)
            self.policy.authorize(ctx, spec)
            if ctx.settings.prefs.mode == 'manual':
                future = asyncio.get_running_loop().create_future()
                ctx.agent.approvals[tid] = (ctx.sid, future)
                self_event('approval_required', args=safe_args)
                try:
                    if not await future:
                        raise ScopeDenied('User declined this tool call')
                finally:
                    ctx.agent.approvals.pop(tid, None)
                # Approval cannot override expired/revoked scope.
                if spec.requires_target_scope:
                    ctx.scope.check()
            self_event('tool_started', args=safe_args)
            ctx.agent.event(ctx.sid, ctx.rid, 'assistant_status', value='running tool')
            timeout = spec.timeout_s
            expiry = ctx.scope.data.get('expires_at', 'session_end')
            if spec.requires_target_scope and expiry != 'session_end':
                timeout = min(timeout, max(0, float(expiry)-now()))
            async with asyncio.timeout(timeout):
                if spec.requires_target_scope:
                    async with self.target_lock:
                        result = await spec.run(ctx, arguments)
                else:
                    result = await spec.run(ctx, arguments)
            outcome = 'success' if result.ok else 'failed'
            if result.stdout and not spec.binary:
                ctx.output('stdout', result.stdout)
            if result.stderr:
                ctx.output('stderr', result.stderr)
        except ScopeDenied as e:
            result = ToolResult(ok=False, denied_reason=clean_text(str(e)), summary=clean_text(str(e)))
            outcome = 'denied'
            self_event('tool_denied', reason=result.denied_reason)
            if not ctx.scope.data:
                self_event('scope_required', reason=result.denied_reason)
        except asyncio.CancelledError:
            outcome = 'cancelled'
            result = ToolResult(ok=False, summary='Cancelled; partial output retained in activity')
            ctx.db.message(ctx.sid, 'tool', json.dumps({**result.model_dump(), 'tool_id': tid, 'untrusted_observation': True}), metadata={'tool_name': name})
            raise
        except Exception as e:
            result = ToolResult(ok=False, summary=clean_text(str(e))[:1200] or type(e).__name__)
            self_event('tool_error', message=result.summary)
        finally:
            result.elapsed_ms = int((now()-started)*1000)
            safe_result = clean_value(result.model_dump())
            result = ToolResult.model_validate(safe_result)
            ctx.db.execute('UPDATE tool_events SET output_json=?,exit_code=?,status=?,ended_at=? WHERE id=?', (result.model_dump_json(), result.exit_code, outcome, now(), tid))
            self_event('tool_finished', **result.model_dump(), status=outcome)
            ctx.db.execute('''DELETE FROM tool_events WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)
                AND id NOT IN (SELECT t.id FROM tool_events t JOIN runs r ON r.id=t.run_id
                WHERE r.session_id=? ORDER BY t.started_at DESC LIMIT 1000)''', (ctx.sid, ctx.sid))
        return result, tid
