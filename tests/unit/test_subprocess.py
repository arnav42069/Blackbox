import asyncio
import os
import signal
import sys

import pytest

from blackbox.tools.runner import SubprocessRunner


@pytest.fixture
def runner():
    r = SubprocessRunner()
    # Only tests inject a fixture executable; model callers cannot modify this mapping.
    r.binaries['fixture'] = sys.executable
    return r


async def test_output_cap(runner):
    r=await runner.run('fixture', ['-c', 'print("x"*1000000)'],cap=1000)
    assert r.truncated and len(r.stdout.encode())<=1000 and not r.ok


async def test_timeout(runner):
    r=await runner.run('fixture',['-c','import time;time.sleep(60)'],timeout=.1)
    assert not r.ok and 'timed out' in r.summary


async def test_cancel_process_tree(runner, tmp_path):
    marker=tmp_path/'child'
    code=f'import subprocess,time; p=subprocess.Popen([{sys.executable!r},"-c","import time; time.sleep(60)"]);open({str(marker)!r},"w").write(str(p.pid));time.sleep(60)'
    task=asyncio.create_task(runner.run('fixture',['-c',code]))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(.01)
    pid=int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.05)
    proc=f'/proc/{pid}/stat'
    assert not os.path.exists(proc) or open(proc).read().split()[2]=='Z'


async def test_no_arbitrary_binary(runner):
    r=await runner.run('/bin/bash',['-c','id'])
    assert not r.ok and 'Missing binary' in r.summary
