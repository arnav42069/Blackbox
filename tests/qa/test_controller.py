"""Controller safety, real process shutdown, privacy, classification and fixed demo oracle."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from scripts.qa_loop import Loop, Halt, Intervention, category, junit_results, foreign_writers
from scripts.qa_metrics import failure_count
from scripts.qa_support import sanitize, agent_findings


def test_failure_metrics_exclude_skipped_results():
    assert failure_count([{'status': 'passed'}, {'status': 'skipped'}, {'status': 'failed'}]) == 1
    assert failure_count([{'status': 'skipped'}]) == 0


def test_secret_redaction(monkeypatch):
    monkeypatch.setenv('FAKE_API_KEY', 'synthetic-env-value')
    data = sanitize({'token': 'fixture-value', 'images': ['large-image-data'], 'nested': {'authorization': 'Bearer fixture-secret'}, 'text': 'password="fixture-secret" synthetic-env-value #pair=fixture-pair'})
    text = json.dumps(data)
    for secret in ('fixture-value', 'large-image-data', 'fixture-secret', 'synthetic-env-value', 'fixture-pair'):
        assert secret not in text


def test_failure_categories_and_junit(tmp_path):
    xml = tmp_path / 'tests.xml'
    xml.write_text('<testsuites><testsuite><testcase classname="tests.integration.test_api" name="test_scope"><failure>scope failed</failure></testcase><testcase classname="tests.unit.test_host" name="test_optional"><skipped message="missing binary"/></testcase></testsuite></testsuites>')
    results = junit_results(xml)
    assert results[0]['id'] == 'tests/integration/test_api.py::test_scope'
    assert results[0]['category'] == 'scope_enforcement'
    assert results[1]['status'] == 'skipped'
    assert category('test_completion') == 'premature_completion'
    assert category('e2e clipboard') == 'ui_failure'


def test_oracle_detects_missing_fabricated_and_cancelled_evidence():
    findings = agent_findings([], [{'id': 'invented', 'tool_name': 'host.preflight', 'status': 'success'}], required_tools=['workspace.read'], expected_text='nonce', final_text='made up', terminal='cancelled')
    assert {f['category'] for f in findings} == {'missing_tool_call', 'fabricated_tool_output', 'broken_cancellation'}


def test_writer_lease_is_exclusive(tmp_path):
    path = tmp_path / 'lease'
    with path.open('w') as first, path.open('w') as second:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_diff_guard_refuses_policy_and_test_changes(tmp_path, monkeypatch):
    import scripts.qa_loop as module
    loop = object.__new__(Loop)
    loop.args = argparse.Namespace(demo=False)
    monkeypatch.setattr(module, 'git', lambda root, *args: 'backend/blackbox/core/policy.py' if '--name-only' in args else '')
    with pytest.raises(Intervention, match='protected'):
        loop.validate_diff(tmp_path, 'base')


def test_process_group_shutdown_stops_descendants(tmp_path):
    marker = tmp_path / 'child'
    code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);open(sys.argv[1],"w").write(str(p.pid));time.sleep(60)'
    process = subprocess.Popen([sys.executable, '-c', code, str(marker)], start_new_session=True)
    try:
        import time
        for _ in range(100):
            if marker.exists(): break
            time.sleep(.01)
        child = int(marker.read_text())
        Loop.kill(process)
        state = Path(f'/proc/{child}/stat')
        assert not state.exists() or state.read_text().split()[2] == 'Z'
    finally:
        if process.poll() is None: Loop.kill(process)


def test_foreign_codex_writer_is_detected(tmp_path):
    # Set only this synthetic child's process name; no actual worker or repository edits.
    code = 'import ctypes,time;ctypes.CDLL(None).prctl(15,b"codex",0,0,0);print("ready",flush=True);time.sleep(60)'
    process = subprocess.Popen([sys.executable, '-c', code], cwd=tmp_path, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        assert process.pid in foreign_writers(tmp_path)
    finally:
        Loop.kill(process)


def test_checkpoint_preserves_dirty_index_and_working_files(tmp_path):
    from scripts.qa_loop import checkpoint, git
    import hashlib
    def run(*args):
        return git(tmp_path, *args)
    run('init', '-b', 'main')
    (tmp_path / '.gitignore').write_text('.runtime/\n')
    source = tmp_path / 'README.md'
    source.write_text('initial\n')
    run('add', '.')
    run('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'initial')
    source.write_text('staged work\n')
    run('add', 'README.md')
    source.write_text('unstaged work\n')
    before = (tmp_path / '.git/index').read_bytes()
    head = run('rev-parse', 'HEAD')
    runtime = tmp_path / '.runtime'
    runtime.mkdir()
    workspace, commit, branch = checkpoint(tmp_path, runtime, 'fixture')
    assert source.read_text() == 'unstaged work\n'
    assert (tmp_path / '.git/index').read_bytes() == before
    assert run('rev-parse', 'HEAD') == head
    assert (workspace / 'README.md').read_text() == 'unstaged work\n'
    assert run('show', ':README.md') == 'staged work'


def test_auto_port_avoids_live_listener():
    import socket
    from scripts.qa_loop import available_port
    # Explicit ports remain explicit; auto chooses an unoccupied loopback endpoint.
    assert available_port('8876') == 8876
    port = available_port('auto')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
    assert 1024 <= port <= 65535


def test_snapshot_preserves_app_fixes_and_excludes_runtime(tmp_path):
    from scripts.qa_loop import snapshot_sources, source_manifest
    root = tmp_path / 'development'
    (root/'backend/blackbox').mkdir(parents=True)
    (root/'backend/blackbox/host.py').write_text('developer_fix = True\n')
    (root/'frontend/dist').mkdir(parents=True)
    (root/'frontend/dist/user-build.js').write_text('do not reuse')
    (root/'backend/blackbox/__pycache__').mkdir()
    (root/'backend/blackbox/__pycache__/secret.pyc').write_bytes(b'private')
    (root/'.runtime').mkdir()
    (root/'.runtime/pair').write_text('private-pairing')
    (root/'.env').write_text('PRIVATE=value')
    destination = tmp_path/'frozen'
    before = snapshot_sources(root, destination)
    assert (destination/'backend/blackbox/host.py').read_text() == 'developer_fix = True\n'
    assert not (destination/'frontend/dist').exists()
    assert not (destination/'.runtime').exists()
    assert not (destination/'.env').exists()
    assert before == source_manifest(root) == source_manifest(destination)


def test_demo_guard_does_not_allow_oracle_edits(tmp_path, monkeypatch):
    import scripts.qa_loop as module
    loop = object.__new__(Loop)
    loop.args = argparse.Namespace(demo=True)
    monkeypatch.setattr(module, 'git', lambda root, *args: 'tests/qa/test_controller.py' if '--name-only' in args else '')
    with pytest.raises(Intervention, match='protected'):
        loop.validate_diff(tmp_path, 'base')
