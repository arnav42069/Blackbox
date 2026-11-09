"""Read-only host inventory. Values come only from fixed local command outputs."""
from __future__ import annotations

import csv
import io
import json
import os
import shlex
from typing import Literal

from pydantic import Field

from .base import Arguments, ToolResult
from ..storage.db import now

# Neither the model nor config can supply argv, executables, paths or environment.
CHECKS = {
    'system': (('uname', ('-srm',)), ('cat', ('/etc/os-release',))),
    'memory': (('free', ('--bytes',)),),
    'cpu': (('lscpu', ('--json',)),),
    'gpu': (('nvidia-smi', ('--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits')), ('lspci', ('-nn',))),
    'tools': (('ping', ('-V',)), ('nmap', ('--version',)), ('ffuf', ('-V',)), ('ssh', ('-V',)), ('curl', ('--version',))),
}


class Diagnostics(Arguments):
    sections: list[Literal['system', 'memory', 'cpu', 'gpu', 'tools']] = Field(default_factory=lambda: list(CHECKS), min_length=1, max_length=5)


def _cpu_fields(entries):
    fields = {}
    for item in entries:
        fields[item.get('field', '').rstrip(':')] = item.get('data')
        fields.update(_cpu_fields(item.get('children', [])))
    return fields


def _integer(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def snapshot(outputs):
    """No PRD defaults, fixtures, platform assumptions or inferred hardware fallbacks."""
    facts = {'source': 'local_host_commands', 'captured_at': now(), 'os': None, 'kernel': None, 'architecture': None,
             'distribution': None, 'memory': None, 'cpu': None, 'gpus': None, 'gpu_controllers': None, 'versions': {}}
    if text := outputs.get('uname'):
        parts = text.split()
        if len(parts) == 3:
            facts.update(os=parts[0], kernel=parts[1], architecture=parts[2])
    if text := outputs.get('cat'):
        release = {}
        for line in text.splitlines():
            key, sep, value = line.partition('=')
            if sep and key in {'PRETTY_NAME', 'ID', 'ID_LIKE', 'VERSION_ID'}:
                try:
                    release[key] = ' '.join(shlex.split(value))
                except ValueError:
                    continue
        facts['distribution'] = release or None
    if text := outputs.get('free'):
        for line in text.splitlines():
            cells = line.split()
            if cells and cells[0] == 'Mem:' and len(cells) >= 7:
                facts['memory'] = {'total_bytes': _integer(cells[1]), 'available_bytes': _integer(cells[6])}
    if text := outputs.get('lscpu'):
        try:
            fields = _cpu_fields(json.loads(text)['lscpu'])
            facts['cpu'] = {'model': fields.get('Model name'), 'logical_cpus': _integer(fields.get('CPU(s)')),
                            'cores_per_socket': _integer(fields.get('Core(s) per socket')), 'sockets': _integer(fields.get('Socket(s)')),
                            'threads_per_core': _integer(fields.get('Thread(s) per core'))}
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    if text := outputs.get('nvidia-smi'):
        cards = []
        for row in csv.reader(io.StringIO(text)):
            if len(row) == 3:
                cards.append({'name': row[0].strip(), 'memory_total_mib': _integer(row[1].strip()), 'driver_version': row[2].strip()})
        facts['gpus'] = cards or None
    if text := outputs.get('lspci'):
        facts['gpu_controllers'] = [line for line in text.splitlines() if any(kind in line.lower() for kind in ('vga compatible controller', '3d controller', 'display controller'))]
    for binary in ('ping', 'nmap', 'ffuf', 'ssh', 'curl'):
        if text := outputs.get(binary):
            facts['versions'][binary] = text.splitlines()[0]
    return facts


async def diagnostics(ctx, args):
    outputs, records, raw = {}, [], []
    for section in dict.fromkeys(args.sections):
        for binary, flags in CHECKS[section]:
            result = await ctx.runner.run(binary, list(flags), timeout=3, cap=8192, on_command=ctx.command)
            text = result.stdout or result.stderr
            # Some version programs write successful version output to stderr.
            if result.ok:
                outputs[binary] = text
            record = {'check': binary, 'argv': [ctx.runner.binaries.get(binary) or binary, *flags], 'ok': result.ok,
                      'exit_code': result.exit_code, 'elapsed_ms': result.elapsed_ms, 'truncated': result.truncated}
            if not result.ok:
                record['error'] = result.summary + (': ' + text[:300] if text else '')
            records.append(record)
            raw.append(f'$ {binary} {shlex.join(flags)}\n{text or result.summary}')
    facts = snapshot(outputs)
    facts['binaries'] = {name: bool(ctx.runner.binaries.get(name)) for name in ('ping', 'nmap', 'ffuf', 'ssh', 'curl', 'whatweb')}
    facts['ssh_agent'] = bool(os.environ.get('SSH_AUTH_SOCK'))
    missing = [r['check'] for r in records if not r['ok']]
    # Preserve compact facts in model context; raw command output stays in the audit record.
    return ToolResult(ok=bool(outputs), stdout='\n\n'.join(raw), evidence=[facts, *records],
                      summary=f'Collected {len(records)-len(missing)}/{len(records)} real host diagnostics. Unknown fields were not measured.' + (f' Failed/unavailable: {", ".join(missing)}.' if missing else ''),
                      truncated=any(r['truncated'] for r in records))


async def preflight(ctx, args):
    return await diagnostics(ctx, Diagnostics())
