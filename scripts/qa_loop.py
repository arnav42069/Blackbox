#!/usr/bin/env python3
"""Local, evidence-driven test/fix/retest. No shell interpreter or production DB access."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.qa_support import sanitize, write_json
from scripts.qa_metrics import failure_count

# A repair cannot alter its oracle, permissions, dependencies or the controller.
ALLOWED = ('frontend/src/', 'backend/blackbox/')
PROTECTED = ('backend/blackbox/app.py', 'backend/blackbox/core/agent.py',
             'frontend/src/api.ts', 'frontend/src/components/Dialogs.tsx', 'backend/blackbox/core/policy.py', 'backend/blackbox/core/scope.py',
             'backend/blackbox/api/', 'backend/blackbox/settings.py',
             'backend/blackbox/tools/', 'backend/blackbox/storage/files.py')


class Intervention(RuntimeError):
    pass


class Halt(RuntimeError):
    pass


def ancestry(pid=None):
    found = set()
    pid = pid or os.getpid()
    while pid > 1 and pid not in found:
        found.add(pid)
        try:
            pid = int((Path('/proc') / str(pid) / 'status').read_text().split('PPid:')[1].splitlines()[0])
        except (OSError, ValueError, IndexError):
            break
    return found


def foreign_writers(root, worker_pid=None):
    """Conservative Linux fallback for interactive sessions that don't use qa-edit.sh."""
    own = ancestry()
    writers = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in own:
            continue
        try:
            if proc.joinpath('comm').read_text().strip() != 'codex':
                continue
            argv = proc.joinpath('cmdline').read_bytes().split(b'\0')
            if b'app-server' in argv:  # A transport server is not an editing session.
                continue
            pid = int(proc.name)
            if worker_pid and worker_pid in ancestry(pid):
                continue
            cwd = proc.joinpath('cwd').resolve()
            if cwd == root or root in cwd.parents:
                writers.append(pid)
        except (OSError, ValueError):
            continue
    return sorted(writers)


def ensure_writer(root, worker_pid=None):
    if pids := foreign_writers(root, worker_pid):
        raise Intervention(f'Other Codex writer(s) attached to checkout: {pids}. Exit them; use scripts/qa-edit.sh for interactive work.')


def git(root, *args, env=None):
    result = subprocess.run(['git', '-C', str(root), *args], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise Intervention('Git operation failed: ' + sanitize(result.stderr[:800]))
    return result.stdout.rstrip("\n")


def source_fingerprint(root):
    paths = git(root, 'ls-files', '-c', '-o', '--exclude-standard', '-z').split('\0')
    digest = hashlib.sha256()
    for name in sorted(set(filter(None, paths))):
        path = root / name
        if path.is_symlink():
            data = os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            data = b'<deleted>'
        digest.update(name.encode() + b'\0' + data)
        digest.update(str(path.lstat().st_mode if path.exists() else 0).encode())
    digest.update(git(root, 'status', '--porcelain=v1', '-z').encode())
    return digest.hexdigest()


def checkpoint(root, runtime, run_id):
    """Temporary index snapshots dirty work without touching the user's index or branch."""
    ensure_writer(root)
    git(root, 'rev-parse', '--show-toplevel')
    index = runtime / ('index-' + run_id)
    env = {**os.environ, 'GIT_INDEX_FILE': str(index)}
    try:
        head = git(root, 'rev-parse', 'HEAD')
        git(root, 'read-tree', head, env=env)
        git(root, 'add', '-u', env=env)
        untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
        for name in filter(None, untracked):
            if not (name.startswith(('backend/', 'frontend/', 'scripts/', 'tests/', 'docs/', 'prompts/')) or name in {'README.md', 'pyproject.toml', 'requirements.lock', 'Makefile', '.gitignore', '.env.example', 'config.example.yaml', 'BlackBox_PRD_v1.1_BlackTurq.md'}):
                raise Intervention(f'Unreviewed untracked file prevents safe checkpoint: {name}')
            if any(part.startswith('.') and part not in {'.env.example'} for part in Path(name).parts):
                raise Intervention('Unreviewed hidden file prevents checkpoint')
        if any(untracked):
            git(root, 'add', '--', *filter(None, untracked), env=env)
        tree = git(root, 'write-tree', env=env)
        identity = {**os.environ, 'GIT_AUTHOR_NAME': 'BlackBox QA', 'GIT_AUTHOR_EMAIL': 'qa@localhost', 'GIT_COMMITTER_NAME': 'BlackBox QA', 'GIT_COMMITTER_EMAIL': 'qa@localhost'}
        commit = git(root, 'commit-tree', tree, '-p', head, '-m', f'QA checkpoint {run_id}', env=identity)
        branch = f'qa/repair-{run_id}'
        worktree = runtime / 'worktrees' / run_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(root, 'worktree', 'add', '-b', branch, str(worktree), commit)
        return worktree, commit, branch
    finally:
        index.unlink(missing_ok=True)



SOURCE_ROOT_FILES = {'README.md', 'pyproject.toml', 'requirements.lock', 'Makefile',
    '.gitignore', '.env.example', 'config.example.yaml', 'BlackBox_PRD_v1.1_BlackTurq.md'}
SOURCE_DIRS = ('backend', 'frontend', 'scripts', 'tests', 'docs', 'prompts')
EXCLUDED_PARTS = {'.git', '.runtime', '.venv', 'node_modules', '__pycache__',
    '.pytest_cache', 'dist', 'test-results', 'playwright-report', 'qa-reports'}


def source_files(root):
    files = [root / name for name in SOURCE_ROOT_FILES if (root / name).is_file()]
    for folder in SOURCE_DIRS:
        for path in (root / folder).rglob('*'):
            parts = path.relative_to(root).parts
            if any(part in EXCLUDED_PARTS or part.endswith('.egg-info') for part in parts):
                continue
            if path.name.startswith('.') or path.name == 'ssh_profiles.yaml' or path.suffix in {'.pyc', '.db', '.log'}:
                continue
            if path.is_symlink():
                raise Intervention('Source symlinks require review: ' + str(path.relative_to(root)))
            if path.is_file():
                files.append(path)
    return sorted(set(files))


def source_manifest(root):
    return {str(path.relative_to(root)): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'mode': path.stat().st_mode & 0o777} for path in source_files(root)}


def snapshot_sources(root, destination):
    """Freeze source, never copy private data or reuse the user's build output."""
    before = source_manifest(root)
    destination.mkdir(parents=True, mode=0o700)
    for name, info in before.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != info['sha256']:
            raise Intervention('Source changed while making the test snapshot; rerun on stable source')
    if source_manifest(root) != before:
        raise Intervention('Source changed while making the test snapshot; no repairs applied')
    return before


def available_port(requested):
    """Prefer the product port, otherwise choose a kernel-assigned loopback port."""
    if requested != 'auto':
        return int(requested)
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', 8765))
            return 8765
        except OSError:
            pass
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def init_snapshot_git(root):
    git(root, 'init', '-b', 'main')
    git(root, 'add', '--', '.')
    git(root, '-c', 'user.name=BlackBox QA', '-c', 'user.email=qa@localhost',
        'commit', '-m', 'QA isolated source checkpoint')


def link_dependencies(workspace):
    (workspace / 'frontend/node_modules').symlink_to(ROOT / 'frontend/node_modules', target_is_directory=True)
    (workspace / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)


def category(node, message=''):
    text = (node + ' ' + message).lower()
    for label, words in [
        ('scope_enforcement', ('scope', 'redirect', 'rebinding', 'authorization', 'auth_host')),
        ('broken_cancellation', ('cancel', 'stop', 'timeout', 'process_tree')),
        ('premature_completion', ('completion', 'promised', 'missing_step')),
        ('fabricated_tool_output', ('fabricat', 'grounded', 'host_commands', 'evidence')),
        ('missing_tool_call', ('missing_tool', 'tool_call')),
        ('ui_failure', ('e2e', 'layout', 'clipboard', 'theme', 'typecheck', 'build'))]:
        if any(word in text for word in words):
            return label
    return 'regression'


def junit_results(path):
    results = []
    for node in ET.parse(path).iter('testcase'):
        status = 'passed'
        detail = ''
        for kind in ('failure', 'error', 'skipped'):
            child = node.find(kind)
            if child is not None:
                status = 'skipped' if kind == 'skipped' else 'failed'
                detail = (child.text or child.get('message', ''))[:12000]
                break
        name = node.get('name', '')
        classname = node.get('classname', '')
        # JUnit class names from these Python tests map to normal pytest nodeids.
        nodeid = classname.replace('.', '/') + '.py::' + name
        results.append({'id': nodeid, 'status': status, 'category': category(nodeid, detail), 'detail': sanitize(detail)})
    return results


def playwright_results(path):
    report = json.loads(path.read_text())
    results = []
    def visit(suite):
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                status = test['status']
                results.append({'id': f"e2e:{spec['file']}:{spec['title']}",
                    'status': 'passed' if status == 'expected' else 'skipped' if status == 'skipped' else 'failed',
                    'category': category('e2e ' + spec['title']), 'detail': sanitize(test.get('results', []))})
        for child in suite.get('suites', []):
            visit(child)
    for suite in report.get('suites', []):
        visit(suite)
    for error in report.get('errors', []):
        results.append({'id': 'e2e:runner', 'status': 'failed', 'category': 'infrastructure', 'detail': sanitize(error)})
    return results


class Loop:
    def __init__(self, args):
        self.args = args
        self.runtime = ROOT / '.runtime'
        self.runtime.mkdir(exist_ok=True, mode=0o700)
        self.run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
        self.report = ROOT / 'qa-reports' / self.run_id
        self.report.mkdir(parents=True, mode=0o700)
        self.deadline = time.monotonic() + args.budget_seconds
        self.start = time.monotonic()
        self.stopping = False
        self.stop_request = None
        self.writer_root = ROOT
        self.child = None
        self.stopfile = self.runtime / 'qa-stop'
        self.data = {'schema_version': 1, 'run_id': self.run_id, 'mode': 'report-only' if args.report_only else 'repair',
                     'status': 'running', 'cycles': [], 'repairs': [], 'limits': vars(args),
                     'coverage_limits': ['Physical Wayland clipboard and external labs are not exercised.',
                         'Only synthetic sessions and explicitly scoped localhost fixtures are used.'],
                     'commands': []}

    def save(self):
        self.data['stop_request'] = self.stop_request
        self.data['active_stage'] = self.data.get('active_stage', 'preflight')
        self.data['elapsed_seconds'] = round(time.monotonic() - self.start, 2)
        write_json(self.report / 'report.json', self.data)
        lines = [f'# BlackBox QA — {self.run_id}', '', f"Status: **{self.data['status']}**", '',
                 f"Stop reason: {self.data.get('stop_reason', 'in progress')}", '',
                 '| Cycle | Passed | Failed | Skipped |', '|---|---:|---:|---:|']
        for cycle in self.data['cycles']:
            results = cycle['results']
            lines.append(f"| {cycle['index']} | {sum(r['status']=='passed' for r in results)} | {failure_count(results)} | {sum(r['status']=='skipped' for r in results)} |")
        lines += ['', 'Evidence: per-cycle JUnit, Playwright JSON, screenshots, API/console/network logs, host/tool journals, and bounded worker event logs.', '']
        for cycle in self.data['cycles']:
            for result in cycle['results']:
                if result['status'] != 'passed':
                    lines += [f"- {result['status']}: `{result['id']}` ({result['category']})", str(result.get('detail', ''))[:1200], '']
        lines += ['', *['- ' + item for item in self.data['coverage_limits']]]
        path = self.report / 'report.md'
        path.write_text(sanitize('\n'.join(lines)) + '\n')
        path.chmod(0o600)
        write_json(ROOT / 'qa-reports' / 'latest.json', {'run_id': self.run_id, 'report': str(self.report.relative_to(ROOT) / 'report.json')})

    def check(self):
        if self.stopping or self.stopfile.exists():
            raise Halt('Graceful stop requested')
        if time.monotonic() >= self.deadline:
            raise Halt('Wall-time resource budget exhausted')
        size = sum(p.stat().st_size for p in self.report.rglob('*') if p.is_file())
        if size > self.args.max_report_mb * 1024 * 1024:
            raise Halt('Report disk budget exhausted')

    @staticmethod
    def kill(process):
        # Playwright servers and Chromium may create separate process groups.
        # Collect descendants before their parents exit and they are reparented.
        groups = {process.pid}
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if process.pid in ancestry(int(entry.name)):
                    group = os.getpgid(int(entry.name))
                    if group != os.getpgrp():
                        groups.add(group)
            except (OSError, ValueError):
                pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for group in groups:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, sig)
            if sig == signal.SIGTERM:
                # Give owned services time to flush evidence and close their DB.
                time.sleep(.5)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)

    def command(self, argv, cwd, output, *, env=None, timeout=180, prompt=None, worker=False):
        self.check()
        started = time.monotonic()
        record = {'argv': sanitize(argv), 'cwd': str(cwd.relative_to(ROOT)) if cwd.is_relative_to(ROOT) else '<isolated>', 'exit_code': None, 'log': str(output.relative_to(self.report))}
        self.data['commands'].append(record)
        self.data['active_stage'] = output.stem
        self.save()
        # Bounded capture in a pipe; raw output never lands in a report file.
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.child = process
        if prompt is not None:
            process.stdin.write(prompt.encode())
            process.stdin.close()
        os.set_blocking(process.stdout.fileno(), False)
        captured = bytearray()
        try:
            while True:
                self.check()
                if worker:
                    ensure_writer(self.writer_root, process.pid)
                block = process.stdout.read(65536)
                if block:
                    captured.extend(block)
                    if len(captured) > self.args.max_log_kb * 1024:
                        raise Halt('Command output budget exhausted')
                elif process.poll() is not None:
                    break
                if time.monotonic() - started > min(timeout, self.deadline - started):
                    record['timed_out'] = True
                    self.kill(process)
                    break
                time.sleep(.1)
        finally:
            if process.poll() is None:
                self.kill(process)
            tail = process.stdout.read() or b''
            captured.extend(tail[:max(0, self.args.max_log_kb * 1024 - len(captured))])
            text = captured.decode(errors='replace')
            # Codex reasoning and free-form command output can contain private local data.
            # Retain event metadata, exit codes and usage; never raw model reasoning/stdout.
            if worker:
                events = []
                for line in text.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    safe = {k: event[k] for k in ('type', 'usage') if k in event}
                    if event.get('type') in {'error','turn.failed'}:
                        safe['error'] = sanitize(str(event.get('message', event.get('error', 'Worker failed')))[:1000])
                    item = event.get('item', {})
                    if item:
                        safe['item'] = {k: item[k] for k in ('id', 'type', 'status', 'exit_code') if k in item}
                    events.append(safe)
                write_json(output, {'events': events, 'exit_code': process.returncode, 'raw_output_retained': False})
                tokens = sum(e.get('usage', {}).get('input_tokens', 0) + e.get('usage', {}).get('output_tokens', 0) for e in events)
                self.data['worker_tokens'] = self.data.get('worker_tokens', 0) + tokens
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(sanitize(text))
                output.chmod(0o600)
            record.update(exit_code=process.returncode, elapsed_seconds=round(time.monotonic() - started, 2))
            self.child = None
            self.save()
        return process.returncode

    def environment(self, workspace, directory):
        return {**os.environ, 'PYTHONPATH': str(workspace / 'backend') + os.pathsep + str(workspace),
                'PYTHONDONTWRITEBYTECODE': '1', 'BLACKBOX_QA_EVIDENCE': str(directory / 'evidence'),
                'BLACKBOX_QA_REPORT': str(directory), 'BLACKBOX_E2E_PORT': str(self.args.test_port),
                'BLACKBOX_E2E_RUNTIME': str(self.runtime / 'qa-e2e' / self.run_id / directory.name),
                'PLAYWRIGHT_JSON_OUTPUT_FILE': str(directory / 'playwright.json'),
                'BLACKBOX_QA_PYTHON': str(ROOT / '.venv/bin/python')}

    def suite(self, workspace, index, focused=()):
        self.check()
        directory = self.report / f'cycle-{index}'
        directory.mkdir(mode=0o700)
        env = self.environment(workspace, directory)
        python = str(ROOT / '.venv/bin/python')
        cycle = {'index': index, 'results': [], 'focused': list(focused)}
        self.data['cycles'].append(cycle)
        results = cycle['results']
        for label, targets in ([('focused', list(focused))] if focused else []) + [('backend', ['tests/unit', 'tests/integration', 'tests/qa'])]:
            xml = directory / (label + '.xml')
            code = self.command([python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'scripts.qa_pytest', f'--junitxml={xml}', *targets], workspace, directory / (label + '.log'), env=env)
            parsed = junit_results(xml) if xml.exists() else []
            if label != 'focused':
                results.extend(parsed)
            cycle[label + '_exit_code'] = code
            if code and not any(r['status'] == 'failed' for r in parsed):
                results.append({'id': label + ':runner', 'status': 'failed', 'category': 'infrastructure', 'detail': f'pytest exited {code}; see {label}.log'})
        for label in ('typecheck', 'build'):
            code = self.command(['npm', 'run', label], workspace / 'frontend', directory / (label + '.log'), env=env)
            results.append({'id': label, 'status': 'failed' if code else 'passed', 'category': 'ui_failure', 'detail': f'exit={code}; see {label}.log'})
        # Never kill or pair with an existing user service; fail with an actionable precondition.
        with socket.socket() as sock:
            busy = sock.connect_ex(('127.0.0.1', self.args.test_port)) == 0
        if busy:
            results.append({'id': 'e2e:port', 'status': 'failed', 'category': 'infrastructure', 'detail': f'Port {self.args.test_port} is occupied. Close BlackBox or choose --test-port for an isolated normally paired QA instance.'})
        else:
            code = self.command(['npm', 'run', 'test:e2e', '--', '--reporter=json'], workspace / 'frontend', directory / 'browser.log', env=env)
            path = directory / 'playwright.json'
            parsed = playwright_results(path) if path.exists() else []
            results.extend(parsed)
            if not parsed or (code and not any(r['status'] == 'failed' for r in parsed)):
                results.append({'id': 'e2e:runner', 'status': 'failed', 'category': 'infrastructure', 'detail': f'Browser runner exit={code}; see browser.log'})
        if self.args.model and index == 0:
            code = self.command([python, 'scripts/qa_models.py', self.args.model, str(directory / 'models.json')], workspace, directory / 'models.log', env=env, timeout=self.args.probe_seconds)
            results.append({'id': 'ollama:actual_probes', 'status': 'failed' if code else 'passed', 'category': 'model_capability', 'detail': f'exit={code}; see models.json'})
        elif index == 0:
            self.data['coverage_limits'].append('Real Ollama inference was not requested; use --model NAME to run actual image/tool probes.')
        for artifact in directory.rglob('*'):
            if artifact.is_file() and artifact.suffix in {'.json', '.xml', '.log', '.md'}:
                artifact.write_text(sanitize(artifact.read_text(errors='replace')))
                artifact.chmod(0o600)
            elif artifact.is_file():
                artifact.chmod(0o600)
        self.save()
        return cycle

    def repair(self, workspace, cycle, number):
        ensure_writer(self.writer_root)
        failures = [r for r in cycle['results'] if r['status'] == 'failed']
        # Environment failures and controls require intervention, never edits to weaken tests/policy.
        if any(r['category'] in {'infrastructure', 'scope_enforcement', 'model_capability'} for r in failures):
            raise Intervention('Failure requires environment, scope or model intervention; automated repair refused')
        prompt = ('Fix the grounded failures below in this BlackBox worktree. Read README.md and existing code. '
                  'Only edit ' + ('scripts/qa_metrics.py' if self.args.demo else 'application files under backend/blackbox or frontend/src') + '. '
                  'Do not edit tests, any other QA harness code, security/policy/scope/auth/tool executors, dependencies, config or prompts. '
                  'Do not run git mutations, external targets, sudo, destructive commands, or read user home files. '
                  'Use existing fixed tests as the oracle. No fabricated output or test weakening. '
                  'If a security or destructive change is necessary, stop and explain. Return after the minimal fix.\n'
                  + json.dumps(sanitize(failures)) + '\nFocused rerun targets: ' + json.dumps([r['id'] for r in failures]))
        log = self.report / f'worker-{number}.json'
        self.data['repairs'].append({'cycle': number, 'log': log.name})
        code = self.command(['codex', 'exec', '--ignore-user-config', '--sandbox', 'workspace-write',
                             '--config', 'approval_policy="never"', '--config', 'sandbox_workspace_write.network_access=false',
                             '--config', 'features.multi_agent=false', '--json', '--ephemeral', '-C', str(workspace), '-'],
                            workspace, log, prompt=prompt, timeout=self.args.worker_seconds, worker=True)
        if code:
            raise Intervention(f'Codex worker exited {code}; inspect bounded worker event log')
        if self.data.get('worker_tokens', 0) > self.args.max_worker_tokens:
            raise Halt('Worker token budget exhausted (measured at turn completion)')

    def validate_diff(self, workspace, base):
        names = git(workspace, 'diff', '--name-only', base).splitlines()
        untracked = git(workspace, 'ls-files', '--others', '--exclude-standard').splitlines()
        if untracked:
            raise Intervention('Worker created unreviewed files; retained in worktree for intervention')
        for name in names:
            if self.args.demo and name == 'scripts/qa_metrics.py':
                continue
            if not name.startswith(ALLOWED) or any(name == p or name.startswith(p) for p in PROTECTED):
                raise Intervention(f'Worker modified protected file {name}; changes retained, never applied')
            path = workspace / name
            if not path.is_file() or path.is_symlink():
                raise Intervention('Destructive/deletion/symlink changes require intervention')
        if git(workspace, 'diff', '--summary', base):
            raise Intervention('File creation/deletion/mode changes require intervention')
        patch = git(workspace, 'diff', '--binary', base)
        if len(patch) > 200000:
            raise Intervention('Repair patch exceeds review budget')
        return patch

    def run(self):
        lock = (self.runtime / 'qa-writer.lock').open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Intervention('QA/editor writer lease is already held') from exc
        self.stopfile.unlink(missing_ok=True)
        owner = self.runtime / 'qa-owner.json'
        write_json(owner, {'pid': os.getpid(), 'run_id': self.run_id})
        try:
            workspace, base = ROOT, None
            original = None
            manifest = source_manifest(ROOT)
            self.data['requested_port'] = self.args.test_port
            self.args.test_port = available_port(self.args.test_port)
            self.data['server_url'] = f'http://127.0.0.1:{self.args.test_port}'
            self.data['source_manifest'] = manifest
            if self.args.report_only or self.args.demo:
                frozen = self.runtime / 'qa-runs' / self.run_id
                manifest = snapshot_sources(ROOT, frozen)
                self.data['source_manifest'] = manifest
                self.data['source_snapshot'] = str(frozen.relative_to(ROOT))
                if self.args.report_only:
                    workspace = frozen
                else:
                    # Demo gets independent Git metadata and never applies to the development tree.
                    init_snapshot_git(frozen)
                    workspace, base, branch = checkpoint(frozen, self.runtime, self.run_id)
                    self.writer_root = workspace
                    self.data['checkpoint'] = {'commit': base, 'branch': branch, 'worktree': str(workspace.relative_to(ROOT)), 'repository': str(frozen.relative_to(ROOT))}
                link_dependencies(workspace)
            else:
                ensure_writer(ROOT)
                original = source_fingerprint(ROOT)
                workspace, base, branch = checkpoint(ROOT, self.runtime, self.run_id)
                self.data['checkpoint'] = {'commit': base, 'branch': branch, 'worktree': str(workspace.relative_to(ROOT))}
                link_dependencies(workspace)
            if self.args.demo:
                target = workspace / 'scripts/qa_metrics.py'
                source = target.read_text()
                if "result['status'] == 'failed'" not in source:
                    raise Intervention('Demo seed no longer matches reviewed metrics implementation')
                target.write_text(source.replace("result['status'] == 'failed'", "result['status'] != 'passed'"))
                self.data['demo_seed'] = 'Metrics incorrectly count skipped results as failures; fixed oracle remains unchanged.'
                (self.report / 'demo-seed.patch').write_text(git(workspace, 'diff', base) + '\n')
            previous = None
            for number in range(self.args.max_cycles + 1):
                focused = [r['id'] for r in previous['results'] if r['status'] == 'failed' and r['id'].startswith('tests/') and '.py::' in r['id']] if previous else []
                cycle = self.suite(workspace, number, focused)
                failed = sorted(r['id'] for r in cycle['results'] if r['status'] == 'failed')
                cycle['failure_signature'] = hashlib.sha256(json.dumps(failed).encode()).hexdigest()
                if not failed:
                    if self.args.demo and not previous:
                        raise Intervention('Demo seed did not produce the required failing regression')
                    if previous and cycle.get('focused_exit_code'):
                        raise Intervention('Focused rerun failed despite passing regression; inconsistent evidence')
                    if base:
                        patch = self.validate_diff(workspace, base)
                        (self.report / 'repair.patch').write_text(sanitize(patch) + '\n')
                        if (original is not None and original != source_fingerprint(ROOT)) or source_manifest(ROOT) != manifest:
                            raise Intervention('Original checkout changed during repair; no changes applied')
                        if patch and not self.args.demo:
                            ensure_writer(ROOT)
                            check = subprocess.run(['git', 'apply', '--check', '-'], cwd=ROOT, input=patch+'\n', text=True, capture_output=True)
                            if check.returncode:
                                raise Intervention('Patch conflicts with original checkout; no changes applied')
                            subprocess.run(['git', 'apply', '-'], cwd=ROOT, input=patch+'\n', text=True, check=True)
                            manifest = source_manifest(ROOT)
                            self.data['repair_applied'] = True
                        # The worktree/branch and reports remain available; never reset/check out user work.
                    if source_manifest(ROOT) != manifest:
                        raise Intervention('Source changed during validation; reports describe the frozen snapshot, rerun for the current source')
                    self.data.update(status='passed', stop_reason='All executed checks passed')
                    return 0
                if self.args.report_only:
                    self.data.update(status='failed', stop_reason='Report-only run; repairs disabled')
                    return 1
                if previous:
                    if cycle['failure_signature'] == previous['failure_signature']:
                        raise Halt('Repeated identical failures')
                    if failure_count(cycle['results']) >= failure_count(previous['results']):
                        raise Halt('No measurable improvement in failing test count')
                    if set(failed) - {r['id'] for r in previous['results'] if r['status'] == 'failed'}:
                        raise Halt('Repair introduced new regression failures')
                if number >= self.args.max_cycles:
                    raise Halt('Maximum repair cycles reached')
                # Validate before testing too, so a worker cannot silently change its oracle.
                previous = cycle
                pre_worker = git(workspace, 'diff', '--binary', base)
                tracked = git(workspace, 'ls-files', '-z').split('\0')
                before_files = {name: (workspace/name).read_text(errors='replace') for name in tracked if name and (workspace/name).is_file() and not (workspace/name).is_symlink() and (workspace/name).suffix in {'.py','.ts','.tsx','.css'}}
                self.repair(workspace, cycle, number + 1)
                self.validate_diff(workspace, base)
                import difflib
                worker_patch = ''.join(''.join(difflib.unified_diff(content.splitlines(True), (workspace/name).read_text().splitlines(True), fromfile='a/'+name, tofile='b/'+name)) for name,content in before_files.items() if (workspace/name).read_text(errors='replace') != content)
                patch_file = self.report / f'worker-{number+1}.patch'
                patch_file.write_text(sanitize(worker_patch))
                patch_file.chmod(0o600)
                if git(workspace, 'diff', '--binary', base) == pre_worker:
                    raise Halt('Worker made no measurable source change')
                if (original is not None and original != source_fingerprint(ROOT)) or source_manifest(ROOT) != manifest:
                    raise Intervention('Original checkout changed; stop before retest/apply')
            return 1
        finally:
            owner.unlink(missing_ok=True)
            self.stopfile.unlink(missing_ok=True)
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='At most one repair, followed by focused and full retest')
    parser.add_argument('--max-cycles', type=int, default=5, choices=range(0, 6))
    parser.add_argument('--report-only', action='store_true', help='Execute all tests, never invoke Codex or mutate Git')
    parser.add_argument('--stop', action='store_true', help='Ask the lease-owning loop to stop its own children')
    parser.add_argument('--demo', action='store_true', help='Seed a metrics bug only in the checkpoint worktree, then repair it')
    parser.add_argument('--test-port', default='auto', help='auto (prefer 8765, otherwise free loopback port), or an explicit unprivileged port')
    parser.add_argument('--model', help='Installed local Ollama model to probe with real image/tool requests')
    parser.add_argument('--budget-seconds', type=int, default=900)
    parser.add_argument('--worker-seconds', type=int, default=180)
    parser.add_argument('--probe-seconds', type=int, default=180)
    parser.add_argument('--max-worker-tokens', type=int, default=100000)
    parser.add_argument('--max-report-mb', type=int, default=64)
    parser.add_argument('--max-log-kb', type=int, default=1024)
    args = parser.parse_args()
    if args.stop:
        runtime = ROOT / '.runtime'
        if not (runtime / 'qa-owner.json').exists():
            print('No QA loop is running.')
            return 0
        (runtime / 'qa-stop').touch(mode=0o600)
        print('Graceful stop requested; reports will be preserved.')
        return 0
    if args.test_port != 'auto':
        try:
            if not 1024 <= int(args.test_port) <= 65535:
                raise ValueError
        except ValueError:
            parser.error('--test-port must be auto or an integer between 1024 and 65535')
    if args.demo and args.report_only:
        parser.error('--demo requires repair mode')
    if any(getattr(args, key) <= 0 for key in ('budget_seconds', 'worker_seconds', 'probe_seconds', 'max_worker_tokens', 'max_report_mb', 'max_log_kb')):
        parser.error('Resource limits must be positive')
    if args.once:
        args.max_cycles = min(args.max_cycles, 1)
    loop = Loop(args)
    def stop(signum, frame):
        loop.stopping = True
        loop.stop_request = signal.Signals(signum).name
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        code = loop.run()
    except Intervention as exc:
        loop.data.update(status='intervention_required', stop_reason=str(exc))
        code = 2
    except Halt as exc:
        loop.data.update(status='stopped', stop_reason=str(exc))
        if 'Graceful' in str(exc) and loop.stop_request is None:
            loop.stop_request = '--stop'
        code = 130 if loop.stopping or 'Graceful' in str(exc) else 1
    except Exception as exc:
        loop.data.update(status='harness_error', stop_reason=f'{type(exc).__name__}: {sanitize(str(exc))}')
        code = 2
    finally:
        if loop.child:
            loop.kill(loop.child)
        loop.data['exit_code'] = code
        loop.save()
    print(f"{loop.data['status']}: {loop.data.get('stop_reason')}\nReport: {loop.report / 'report.md'}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
