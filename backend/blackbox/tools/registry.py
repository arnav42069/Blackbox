from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass,field

from pydantic import Field

from . import network, workspace, web, ssh, host, scans
from .base import Arguments, ToolResult, ToolSpec, ToolError
from .validation import normalize, classify_error
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
    intent: str | None = None
    force_rescan: bool = False
    remote_access_required: bool | None = None
    run_target: str | None = None
    require_vpn: bool = False
    preflight_target: str | None = None
    failed_calls: dict = field(default_factory=dict)

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
        self.add('web.discover_paths', 'Check a curated small list of public web paths. Requires active scans.', web.Discover, web.discover, risk='active_scan', target=True, timeout=60)
        self.add('web.technologies', 'Identify web technologies with bounded passive WhatWeb on pinned HTTP.', web.Inspect, web.technologies, target=True, binary='whatweb')
        self.add('web.tls', 'Inspect TLS handshake and certificate verification with OpenSSL; not a full cipher audit.', web.Inspect, web.tls, target=True, binary='openssl')
        self.add('scan.lookup', 'Analyze stored scoped findings, with freshness and provenance; no new scan.', scans.Lookup, scans.lookup)
        self.add('scan.parse', 'Parse an existing Nmap XML, ffuf JSON or terminal export from workspace with its actual observation timestamp.', scans.Parse, scans.parse)
        self.add('ssh.probe', 'Verify existing authorized public-key SSH/SFTP access using a configured profile.', ssh.SSHArgs, ssh.probe, target=True)
        self.add('ssh.list_dir', 'Read-only SFTP listing inside approved lab paths.', ssh.RemotePath, ssh.list_dir, target=True)
        self.add('ssh.find_files', 'Find candidate flag filenames inside approved paths, with depth and entry caps.', ssh.Find, ssh.find_files, target=True)
        self.add('ssh.read_file', 'Read bounded candidate file via SFTP; secret stores forbidden.', ssh.RemoteRead, ssh.read_file, target=True)
        self.add('artifacts.save', 'Save generated text to a new file in this session artifacts folder.', Artifact, save_artifact)

    def add(self, name, description, schema, run, risk='read_only', target=False, timeout=25, binary=None):
        if not issubclass(schema,Arguments):
            raise TypeError('Every registered tool requires a strict typed Arguments schema')
        self.specs[name] = ToolSpec(name, description, schema, risk, target, timeout, run, binary)

    def schemas(self, intent=None):
        return [s.schema() for s in self.specs.values()
                if (not s.binary or shutil.which(s.binary)) and
                (not s.name.startswith('ssh.') or intent not in {'http_discovery','ports','headers','technologies','tls','image','analyze','address_only'})]

    async def execute(self, ctx, name, raw):
        ctx.calls += 1
        tid = ctx.tool_id = uid()
        started = now()
        args = json.loads(json.dumps(raw))
        # Redact user-controlled data before audit logging, including generated report text.
        safe_args = clean_value(args)
        ctx.db.execute('INSERT INTO tool_events VALUES (?,?,?,?,?,NULL,?,?,NULL)', (tid, ctx.rid, name, json.dumps(safe_args), '{}', 'running', started))
        self_event = lambda kind, **payload: ctx.agent.event(ctx.sid, ctx.rid, kind, tool_id=tid, name=name, **payload)
        self_event('tool_arguments_received',args=safe_args)
        result = ToolResult(ok=False)
        outcome = 'failed'
        validated_args = args if isinstance(args,dict) else {}
        arguments_validated=False
        key=json.dumps([name,args],sort_keys=True)
        try:
            spec = self.specs.get(name)
            if not spec:
                raise ScopeDenied('Unregistered tool; arbitrary commands are unavailable')
            prior=ctx.failed_calls.get(key)
            if prior:
                error=prior[0].model_copy(update={'code':'IDENTICAL_FAILURE_SUPPRESSED','previous_evidence_id':prior[1],
                    'message':'Identical failed arguments were not executed again. Inspect the prior structured error and choose a corrected or different permitted action.'})
                result=ToolResult(ok=False,summary=error.message,error=error)
                self_event('tool_retry_suppressed',previous_evidence_id=prior[1],reason=error.message)
                return result,tid
            args=normalize(spec,args,ctx.settings)
            arguments = spec.argument_schema.model_validate(args)
            validated_args = arguments.model_dump()
            arguments_validated=True
            from ..core.scans import normalized_args
            key=json.dumps([name,normalized_args(name,validated_args)],sort_keys=True)
            if args!=raw:
                self_event('tool_arguments_normalized',arguments=clean_value(validated_args),reason='Unambiguous schema-preserving normalization; all policy checks still apply')
            self.policy.authorize(ctx, spec, arguments)
            await self.policy.validate_destination(ctx, spec, arguments)
            prior=ctx.failed_calls.get(key)
            if prior:
                error=prior[0].model_copy(update={'code':'IDENTICAL_FAILURE_SUPPRESSED','previous_evidence_id':prior[1],
                    'message':'This identical call already failed in this run and was not executed again. Correct arguments, inspect evidence, or choose a different permitted action.'})
                result=ToolResult(ok=False,summary=error.message,error=error)
                self_event('tool_retry_suppressed',previous_evidence_id=prior[1],reason=error.message)
                return result,tid
            if spec.binary and not ctx.runner.binaries.get(spec.binary):
                result=ToolResult(ok=False,summary=f'Tool unavailable: {spec.binary}',error=ToolError(kind='unavailable',code='TOOL_UNAVAILABLE',message=f'{spec.binary} is not installed',suggestions=['Use an available registered adapter or ask the operator to install the missing dependency. SSH is not an HTTP-tool fallback.']))
                return result,tid
            self_event('policy_validated', arguments=clean_value(validated_args), reason='Registered schema, intent, action class, scope and configured limits validated independently of model decisions')
            if ctx.settings.prefs.mode == 'manual':
                future = asyncio.get_running_loop().create_future()
                ctx.agent.approvals[tid] = (ctx.sid, future)
                self_event('approval_required', args=clean_value(validated_args), requested_args=safe_args)
                try:
                    if not await future:
                        raise ScopeDenied('User declined this tool call')
                finally:
                    ctx.agent.approvals.pop(tid, None)
                # Approval cannot override expired/revoked scope.
                if spec.requires_target_scope:
                    ctx.scope.check()
            self_event('tool_started', args=clean_value(validated_args), requested_args=safe_args)
            ctx.agent.event(ctx.sid, ctx.rid, 'assistant_status', value='running tool')
            timeout = spec.timeout_s
            expiry = ctx.scope.data.get('expires_at', 'session_end')
            if spec.requires_target_scope and expiry != 'session_end':
                timeout = min(timeout, max(0, float(expiry)-now()))
            async with asyncio.timeout(timeout):
                if spec.requires_target_scope:
                    async with self.target_lock:
                        result = await self.execute_or_reuse(ctx, spec, arguments)
                else:
                    result = await self.execute_or_reuse(ctx, spec, arguments)
            outcome = 'success' if result.ok else 'failed'
            if result.stdout and not spec.binary:
                ctx.output('stdout', result.stdout)
            if result.stderr:
                ctx.output('stderr', result.stderr)
        except ScopeDenied as e:
            result = ToolResult(ok=False, denied_reason=clean_text(str(e)), summary=clean_text(str(e)),error=classify_error(e,name))
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
            error=classify_error(e,name)
            if isinstance(e,FileNotFoundError) and spec and spec.binary:
                error=ToolError(kind='unavailable',code='TOOL_UNAVAILABLE',message=f'Required executable {spec.binary} could not be started',suggestions=['Check the installed dependency and refresh capability detection. Do not substitute unrelated SSH access.'])
            result = ToolResult(ok=False, summary=clean_text(str(e))[:1200] or type(e).__name__,error=error)
            self_event('tool_error', message=result.summary)
        finally:
            result.validated_arguments=validated_args if arguments_validated else None
            if not result.ok and not result.error and outcome!='cancelled':
                transient='timed out' in result.summary.lower() or (name.startswith('web.') and spec.binary=='curl' and result.exit_code in {5,6,7,18,28,52,55,56})
                result.error=ToolError(kind='transient' if transient else 'unavailable' if result.summary.startswith('Missing binary') else 'permanent',code='EXECUTION_FAILED',message=result.summary,retryable=transient,suggestions=['Inspect the recorded failure and choose another permitted action; no success or access was established. Transient retry requires a later explicit run, not an identical automatic replay.'])
            if result.error and result.error.code!='IDENTICAL_FAILURE_SUPPRESSED':
                ctx.failed_calls[key]=(result.error,tid)
            result.elapsed_ms = int((now()-started)*1000)
            for finding in result.evidence:
                finding.setdefault('run_id',ctx.rid)
                finding.setdefault('observed_at',started)
                finding.setdefault('authorized_target',ctx.run_target or validated_args.get('target') or validated_args.get('url'))
                if result.cached_scan_id:
                    finding['reused_in_run_id']=ctx.rid
            safe_result = clean_value(result.model_dump())
            result = ToolResult.model_validate(safe_result)
            if name in {'net.scan_ports', 'web.discover_paths', 'web.inspect', 'web.technologies', 'web.tls'} and not result.cached_scan_id:
                from ..core.scans import ScanStore
                ScanStore(ctx.db).save(ctx.sid, ctx.scope.data, name, validated_args, result.evidence,
                    status=outcome, complete=result.ok and not result.truncated, evidence_id=tid,
                    details={'result':result.model_dump()})
            ctx.db.execute('UPDATE tool_events SET output_json=?,exit_code=?,status=?,ended_at=? WHERE id=?', (result.model_dump_json(), result.exit_code, outcome, now(), tid))
            self_event('tool_finished', **result.model_dump(), status=outcome)
            ctx.db.execute('''DELETE FROM tool_events WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)
                AND id NOT IN (SELECT t.id FROM tool_events t JOIN runs r ON r.id=t.run_id
                WHERE r.session_id=? ORDER BY t.started_at DESC LIMIT 1000)''', (ctx.sid, ctx.sid))
        return result, tid

    async def execute_or_reuse(self, ctx, spec, arguments):
        # Recheck under the target lock: concurrent equivalent requests execute only once.
        self.policy.authorize(ctx, spec, arguments)
        await self.policy.validate_destination(ctx, spec, arguments)
        if spec.name in {'net.scan_ports', 'web.discover_paths'} and not ctx.force_rescan:
            from ..core.scans import ScanStore
            cached = await ScanStore(ctx.db).cached(ctx, spec.name, arguments.model_dump())
            if cached:
                row, saved = cached
                result = ToolResult.model_validate(saved)
                source=ctx.db.one('SELECT run_id FROM tool_events WHERE id=?',(row['evidence_id'],))
                for finding in result.evidence:
                    finding.setdefault('run_id',source['run_id'] if source else None)
                    finding.setdefault('observed_at',row['observed_at'])
                result.cached_scan_id = row['id']
                result.summary = f"Reused fresh complete scan {row['id']} observed at {row['observed_at']}: " + result.summary
                ctx.agent.event(ctx.sid, ctx.rid, 'scan_reused', tool_id=ctx.tool_id, scan_id=row['id'], reason='Equivalent validated request, same session and authorization scope, within configured freshness')
                return result
        if ctx.run_target and spec.requires_target_scope and ctx.preflight_target!=ctx.run_target:
            preflight=await network.preflight(ctx,ctx.run_target)
            ctx.agent.event(ctx.sid,ctx.rid,'target_preflight',target=ctx.run_target,observations=preflight.evidence,reason=preflight.summary)
            if not preflight.ok:
                return preflight
            ctx.preflight_target=ctx.run_target
        return await spec.run(ctx, arguments)
