"""Use actual installed commands and production Registry; no Ollama/tool mock results."""
import asyncio
import csv
import io
import json
import os
import shutil

import pytest

from blackbox.tools.registry import Registry, ToolContext
from blackbox.core.scope import Scope
from blackbox.storage.db import Database, uid, now


async def direct(binary, *args):
    path = shutil.which(binary)
    if not path:
        return None
    process = await asyncio.create_subprocess_exec(path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C.UTF-8'})
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    return stdout.decode() if process.returncode == 0 else None


async def test_production_preflight_agrees_with_real_host_commands(settings):
    settings.prepare()
    db = Database(settings.data_dir/'blackbox.db')
    sid = db.create_session('unused-model')['id']
    rid = uid()
    db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')", (rid,sid,now()))
    class Audit:
        def event(self, sid, rid, kind, **payload): return db.event(sid,rid,kind,payload)
    registry = Registry()
    context = ToolContext(settings, db, sid, rid, Scope(None), registry.runner, Audit())
    try:
        result, tid = await registry.execute(context, 'host.preflight', {})
        assert result.ok
        facts = result.evidence[0]
        assert facts['source'] == 'local_host_commands'
        uname = await direct('uname', '-srm')
        if uname:
            assert facts['kernel'] == uname.split()[1] == os.uname().release
        cpu = await direct('lscpu', '--json')
        if cpu:
            assert facts['cpu']['model'] in cpu
            assert facts['cpu']['logical_cpus'] == os.cpu_count()
        memory = await direct('free', '--bytes')
        if memory:
            memline = next(line.split() for line in memory.splitlines() if line.startswith('Mem:'))
            assert facts['memory']['total_bytes'] == int(memline[1])
            # Available RAM changes while tests run; compare it to the exact audited capture.
            recorded = result.stdout.split('$ free --bytes\n')[1].split('\n\n$ ')[0]
            captured = next(line.split() for line in recorded.splitlines() if line.startswith('Mem:'))
            assert facts['memory']['available_bytes'] == int(captured[6])
        gpu = await direct('nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits')
        if gpu:
            row = next(csv.reader(io.StringIO(gpu)))
            assert facts['gpus'][0]['name'] == row[0].strip()
            assert facts['gpus'][0]['memory_total_mib'] == int(row[1])
        pci = await direct('lspci','-nn')
        if pci:
            assert all(line in pci for line in facts['gpu_controllers'])
        events = db.events(sid)
        commands = [e['argv'] for e in events if e['type']=='tool_command']
        for record in result.evidence[1:]:
            if registry.runner.binaries.get(record['check']):
                assert record['argv'] in commands
        stored = json.loads(db.one('SELECT output_json FROM tool_events WHERE id=?',(tid,))['output_json'])
        assert stored['evidence'][0] == facts
        assert context.calls == 1  # Normal policy/budget accounting still applies.
    finally:
        db.close()
