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
from scripts.qa_focus import REPAIR_SCHEMA, concise_failures, failure_excerpts, failure_groups, relevant_sources, token_usage

# A repair cannot alter its oracle, permissions, dependencies or the controller.
ALLOWED = ('frontend/src/', 'backend/blackbox/')
PROTECTED = ('backend/blackbox/app.py', 'backend/blackbox/core/agent.py',
             'frontend/src/api.ts', 'frontend/src/components/Dialogs.tsx', 'backend/blackbox/core/policy.py', 'backend/blackbox/core/scope.py',
             'backend/blackbox/api/', 'backend/blackbox/settings.py',
             'backend/blackbox/tools/', 'backend/blackbox/storage/files.py')
# Completion, rendering and approval UI also contain security controls.
PROTECTED += ('backend/blackbox/core/completion.py', 'backend/blackbox/storage/images.py',
              'backend/blackbox/storage/retention.py', 'backend/blackbox/cli.py',
              'frontend/src/components/Messages.tsx', 'frontend/src/components/Activity.tsx',
              'frontend/src/app.tsx')


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
        properties = {p.get('name'): p.get('value') for p in node.findall('properties/property')}
        nodeid = properties.get('qa_nodeid') or classname.replace('.', '/') + '.py::' + name
        results.append({'id': nodeid, 'status': status, 'category': category(nodeid, detail), 'detail': sanitize(detail)})
    return results


def playwright_results(path):
    report = json.loads(path.read_text())
    results = []
    def visit(suite):
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                status = test['status']
                project = test.get('projectName', '')
                results.append({'id': f"e2e:{spec['file']}:{spec.get('line', 0)}:{spec['title']}" + (':' + project if project else ''),
                    'file': spec['file'], 'line': spec.get('line'), 'title': spec['title'], 'project': project,
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
                     'commands': [], 'failure_groups': [], 'worker_tokens': 0,
                     'worker_usage_complete': True}

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
        lines += ['', f"Measured worker tokens: {self.data['worker_tokens']} (usage complete: {self.data['worker_usage_complete']})."]
        for repair in self.data['repairs']:
            lines.append(f"- {repair['group']} attempt {repair['attempt']}: {repair['reasoning']}, {repair['result']}; "
                         f"tokens={repair.get('tokens', 'unavailable')}, seconds={repair.get('elapsed_seconds', 'pending')}")
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
                self.worker_edits = None
                for line in text.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    safe = {k: event[k] for k in ('type', 'usage') if k in event}
                    if event.get('type') in {'error','turn.failed'}:
                        safe['error'] = sanitize(str(event.get('message', event.get('error', 'Worker failed')))[:1000])
                    item = event.get('item', {})
                    if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
                        try:
                            proposal = json.loads(item.get('text', ''))
                            self.worker_edits = proposal.get('edits') if isinstance(proposal, dict) else None
                        except ValueError:
                            self.worker_edits = None
                    if item:
                        safe['item'] = {k: item[k] for k in ('id', 'type', 'status', 'exit_code') if k in item}
                    events.append(safe)
                write_json(output, {'events': events, 'exit_code': process.returncode, 'raw_output_retained': False})
                usage = token_usage(events)
                record['token_usage'] = usage
                record['usage_complete'] = usage is not None and process.returncode == 0
                if not record['usage_complete']:
                    self.data['worker_usage_complete'] = False
                if usage is not None:
                    self.data['worker_tokens'] += usage['total_tokens']
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

    def suite(self, workspace, index, *, stage='regression', targets=None):
        self.check()
        directory = self.report / f'cycle-{index}'
        directory.mkdir(mode=0o700)
        env = self.environment(workspace, directory)
        python = str(ROOT / '.venv/bin/python')
        cycle = {'index': index, 'stage': stage, 'results': [], 'targets': targets}
        self.data['cycles'].append(cycle)
        results = cycle['results']
        selection = dict(targets) if targets is not None else {
            'pytest': ['tests/unit', 'tests/integration', 'tests/qa'],
            'frontend': ['typecheck', 'build'], 'browser': [[]]}
        # Browser fixtures serve dist/, so rebuilding is a prerequisite, not a retest.
        frontend = list(selection.get('frontend', []))
        if selection.get('browser') and 'build' not in frontend:
            frontend.append('build')
        for label, nodes in ([('backend', selection['pytest'])] if selection.get('pytest') else []):
            xml = directory / (label + '.xml')
            code = self.command([python, '-m', 'pytest', '-q', '--tb=short', '-p', 'no:cacheprovider', '-p', 'scripts.qa_pytest', f'--junitxml={xml}', *nodes], workspace, directory / (label + '.log'), env=env)
            parsed = junit_results(xml) if xml.exists() else []
            results.extend(parsed)
            cycle[label + '_exit_code'] = code
            if not parsed or (code and not any(r['status'] == 'failed' for r in parsed)):
                results.append({'id': label + ':runner', 'status': 'failed', 'category': 'infrastructure', 'detail': f'pytest exited {code}; see {label}.log'})
        build_failed = False
        for label in frontend:
            code = self.command(['npm', 'run', label], workspace / 'frontend', directory / (label + '.log'), env=env)
            results.append({'id': label, 'status': 'failed' if code else 'passed', 'category': 'ui_failure',
                            'detail': (directory / (label + '.log')).read_text()[-6000:] if code else f'exit={code}'})
            if label == 'build' and code:
                build_failed = True
        # Never kill or pair with an existing user service; fail with an actionable precondition.
        for browser_index, selectors in enumerate(selection.get('browser', [])):
            if build_failed:
                results.append({'id': 'e2e:build', 'status': 'skipped', 'category': 'ui_failure',
                                'detail': 'Browser checks require a successful build; stale output is never used.'})
                break
            with socket.socket() as sock:
                busy = sock.connect_ex(('127.0.0.1', self.args.test_port)) == 0
            if busy:
                results.append({'id': 'e2e:port', 'status': 'failed', 'category': 'infrastructure', 'detail': f'Port {self.args.test_port} is occupied. Close BlackBox or choose --test-port for an isolated normally paired QA instance.'})
                break
            env['PLAYWRIGHT_JSON_OUTPUT_FILE'] = str(directory / f'playwright-{browser_index}.json') if selectors else str(directory / 'playwright.json')
            code = self.command(['npm', 'run', 'test:e2e', '--', '--reporter=json', *selectors], workspace / 'frontend', directory / f'browser{browser_index or ""}.log', env=env)
            path = Path(env['PLAYWRIGHT_JSON_OUTPUT_FILE'])
            parsed = playwright_results(path) if path.exists() else []
            results.extend(parsed)
            if not parsed or (code and not any(r['status'] == 'failed' for r in parsed)):
                results.append({'id': 'e2e:runner', 'status': 'failed', 'category': 'infrastructure', 'detail': f'Browser runner exit={code}; see browser.log'})
        if self.args.model and targets is None:
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

    @staticmethod
    def focused_targets(group):
        if group['kind'] == 'pytest':
            return {'pytest': [r['id'] for r in group['failures']]}
        if group['kind'] == 'browser':
            # A source line can generate several tests; title + project makes it exact.
            return {'browser': [[f"{re.escape(r['file'])}:{r['line']}", '--grep', re.escape(r['title']) + '$',
                                 *(['--project', r['project']] if r.get('project') else [])]
                                for r in group['failures']]}
        if group['kind'] == 'frontend':
            return {'frontend': [r['id'] for r in group['failures']]}
        raise Intervention('No reliable focused command for ' + group['id'])

    @staticmethod
    def subsystem_targets(group, changed):
        selection = {}
        if group['kind'] == 'pytest':
            selection['pytest'] = [str(Path(group['target']).parent)]
        if any(name.startswith('backend/') for name in changed):
            # Shared app fixtures and dynamic tool dispatch cross Python test directories.
            selection['pytest'] = ['tests/unit', 'tests/integration', 'tests/qa']
        elif any(name.startswith('scripts/') for name in changed):
            selection['pytest'] = sorted(set(selection.get('pytest', []) + ['tests/qa']))
        if group['kind'] in {'browser', 'frontend'} or any(name.startswith('frontend/') for name in changed):
            selection.update(frontend=['typecheck', 'build'], browser=[[]])
        return selection

    def repair(self, workspace, group, number):
        ensure_writer(self.writer_root)
        failures = group['failures']
        # Environment failures and controls require intervention, never edits to weaken tests/policy.
        if any(r['category'] in {'infrastructure', 'scope_enforcement', 'model_capability'} for r in failures):
            raise Intervention('Failure requires environment, scope or model intervention; automated repair refused')
        remaining = self.args.max_worker_tokens - self.data['worker_tokens']
        if remaining <= 0:
            raise Halt('Worker token budget exhausted')
        reasoning = 'high' if group['attempts'] == 2 else self.args.worker_reasoning
        prompt = ('Propose the smallest fix for this reproduced failure group. '
                  'Return JSON matching the supplied schema: edits with path, exact old text and replacement new text. '
                  'Each old text must occur exactly once. Do not edit files yourself or use an editing tool. '
                  'Only propose edits to ' + ('scripts/qa_metrics.py' if self.args.demo else 'application files under backend/blackbox or frontend/src') + '. '
                  'Do not edit tests, any other QA harness code, security/policy/scope/auth/tool executors, dependencies, config or prompts. '
                  'Do not run git mutations, external targets, sudo, destructive commands, or read user home files. '
                  'Use existing fixed tests as the oracle. No fabricated output or test weakening. '
                  'If a security or destructive change is necessary, stop and explain. '
                  'Current test/source excerpts are supplied below; use them directly without rereading those files. '
                  'Batch any necessary additional reads in one command. Return replacements immediately when excerpts suffice. '
                  'The controller owns all testing and Git checks; do not run tests, git commands or inspect QA history. '
                  f'Use at most {remaining} input+output tokens; return after the minimal fix.\n'
                  + json.dumps(sanitize({'failures': concise_failures(failures),
                      'focused_targets': self.focused_targets(group), 'source_files': group['source_files'],
                      'excerpts': failure_excerpts(workspace, failures, group['source_files']),
                      'previous_attempt': group.get('last_result')})))
        if len(prompt) > 24000:
            raise Intervention('Failure group exceeds concise worker context budget; split requires review')
        log = self.report / f'worker-{number}.json'
        schema = self.report / 'repair-schema.json'
        write_json(schema, REPAIR_SCHEMA)
        record = {'cycle': number, 'group': group['id'], 'attempt': group['attempts'], 'reasoning': reasoning,
                  'remaining_token_budget': remaining, 'log': log.name, 'result': 'running',
                  'prompt_bytes': len(prompt.encode())}
        self.data['repairs'].append(record)
        started = time.monotonic()
        self.worker_edits = None
        try:
            code = self.command(['codex', 'exec', '--ignore-user-config', '--sandbox', 'workspace-write',
                             '--config', 'approval_policy="never"', '--config', 'sandbox_workspace_write.network_access=false',
                             '--config', f'model_reasoning_effort="{reasoning}"',
                             '--config', 'features.multi_agent=false', '--output-schema', str(schema),
                             '--json', '--ephemeral', '-C', str(workspace), '-'],
                            workspace, log, prompt=prompt, timeout=self.args.worker_seconds, worker=True)
            usage = self.data['commands'][-1].get('token_usage')
            record.update(exit_code=code, usage=usage, tokens=usage['total_tokens'] if usage else None)
            if code:
                raise Intervention(f'Codex worker exited {code}; inspect bounded worker event log')
            if not usage:
                raise Intervention('Worker usage unavailable; cannot verify token budget')
            if self.data['worker_tokens'] > self.args.max_worker_tokens:
                raise Halt('Worker token budget exhausted (measured at turn completion)')
            record['proposal_edits'] = self.apply_worker_edits(workspace, self.worker_edits)
            record['result'] = 'awaiting_validation'
        except BaseException:
            record['result'] = 'worker_failed_or_budget_stopped'
            raise
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 2)
            self.save()

    def allowed_repair_path(self, name):
        if Path(name).is_absolute() or Path(name).as_posix() != name or any(part.startswith('.') for part in Path(name).parts):
            return False
        if self.args.demo:
            return name == 'scripts/qa_metrics.py'
        return name.startswith(ALLOWED) and not any(name == p or name.startswith(p) for p in PROTECTED)

    def apply_worker_edits(self, workspace, edits):
        """Validate the entire structured proposal before writing any source."""
        if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
            raise Intervention('Worker returned no valid bounded repair proposal')
        if len(json.dumps(edits)) > 200000:
            raise Intervention('Repair proposal exceeds review budget')
        staged = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {'path', 'old', 'new'} or not all(isinstance(v, str) for v in edit.values()):
                raise Intervention('Malformed worker replacement')
            name, old, new = edit['path'], edit['old'], edit['new']
            path = workspace / name
            if Path(name).is_absolute() or '..' in Path(name).parts or not self.allowed_repair_path(name):
                raise Intervention('Worker proposed protected file ' + name)
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
                raise Intervention('Worker proposed creation/deletion/symlink changes')
            source = staged[name] if name in staged else path.read_text()
            if not old or old == new or source.count(old) != 1:
                raise Intervention('Worker replacement does not uniquely match current source')
            staged[name] = source.replace(old, new, 1)
        for name, source in staged.items():
            (workspace / name).write_text(source)
        return len(edits)

    def repair_groups(self, workspace, baseline, base, stable):
        groups = failure_groups(baseline['results'])
        baseline_failed = {r['id'] for r in baseline['results'] if r['status'] == 'failed'}
        self.data['failure_groups'] = groups
        if any(g['kind'] == 'intervention' or any(r['category'] in {
                'infrastructure', 'scope_enforcement', 'model_capability'} for r in g['failures']) for g in groups):
            raise Intervention('Failure requires environment, scope or model intervention; automated repair refused')
        for group in groups:
            group['source_files'] = relevant_sources(workspace, group['failures'])
            focused = self.focused_targets(group)
            reproduction = self.suite(workspace, len(self.data['cycles']), stage='reproduce', targets=focused)
            expected = {r['id'] for r in group['failures']}
            actual = {r['id'] for r in reproduction['results'] if r['status'] == 'failed'}
            group['reproduction_cycle'] = reproduction['index']
            if self.data['repairs'] and not actual and all(any(r['id'] == node and r['status'] == 'passed'
                                      for r in reproduction['results']) for node in expected):
                # A preceding group's fix can resolve a shared cause.
                group['result'] = 'pending_regression'
                continue
            if actual != expected:
                raise Intervention('Focused reproduction differs from baseline; unreliable or interacting failures')
            group['failures'] = [r for r in reproduction['results'] if r['status'] == 'failed']
            for attempt in (1, 2):
                if len(self.data['repairs']) >= self.args.max_cycles:
                    raise Halt('Maximum repair cycles reached')
                group['attempts'] = attempt
                # Git mutations and oracle edits are checked independently of the diff.
                head = git(workspace, 'rev-parse', 'HEAD')
                index = git(workspace, 'diff', '--cached', '--binary')
                before = source_manifest(workspace)
                before_files = {name: (workspace / name).read_bytes() for name in before}
                self.repair(workspace, group, len(self.data['repairs']) + 1)
                record = self.data['repairs'][-1]
                try:
                    if head != git(workspace, 'rev-parse', 'HEAD') or index != git(workspace, 'diff', '--cached', '--binary'):
                        raise Intervention('Worker mutated Git history or index; changes never applied')
                    self.validate_diff(workspace, base)
                    after = source_manifest(workspace)
                    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
                    record['changed_files'] = changed
                    import difflib
                    worker_patch = ''.join(''.join(difflib.unified_diff(
                        before_files[name].decode(errors='replace').splitlines(True),
                        (workspace / name).read_text(errors='replace').splitlines(True),
                        fromfile='a/' + name, tofile='b/' + name)) for name in changed)
                    patch_file = self.report / f"worker-{record['cycle']}.patch"
                    patch_file.write_text(sanitize(worker_patch))
                    patch_file.chmod(0o600)
                    stable()
                    check = self.suite(workspace, len(self.data['cycles']), stage='focused', targets=focused)
                    record['focused_cycle'] = check['index']
                    failed = [r for r in check['results'] if r['status'] == 'failed']
                    if {r['id'] for r in failed} - expected:
                        raise Halt('Repair introduced new regression failures')
                    if not failed:
                        if not expected <= {r['id'] for r in check['results'] if r['status'] == 'passed'}:
                            raise Intervention('Focused targets missing or skipped; repair cannot be verified')
                        if not changed:
                            raise Intervention('Focused test passed without a source change; inconsistent evidence')
                        affected = self.suite(workspace, len(self.data['cycles']), stage='subsystem',
                                              targets=self.subsystem_targets(group, changed))
                        record['subsystem_cycle'] = affected['index']
                        failed = [r for r in affected['results'] if r['status'] == 'failed']
                        if {r['id'] for r in failed} - baseline_failed:
                            raise Halt('Repair introduced new regression failures')
                        # Other baseline groups are handled in their own bounded turn.
                        failed = [r for r in failed if r['id'] in expected]
                    if not failed:
                        record['result'] = 'focused_and_subsystem_passed'
                        group['result'] = 'pending_regression'
                        break
                    if any(r['category'] in {'infrastructure', 'scope_enforcement', 'model_capability'} for r in failed):
                        raise Intervention('Retest requires environment, scope or model intervention')
                    record['result'] = 'failure_persists'
                    group['last_result'] = 'Failure persists after focused/subsystem retest; make a different minimal fix.'
                    group['failures'] = failed
                    if attempt == 2:
                        raise Halt('Two attempts exhausted for failure group ' + group['id'])
                except BaseException:
                    record['result'] = 'rejected'
                    group['result'] = 'rejected'
                    raise
                finally:
                    self.save()
        # Exactly one complete acceptance regression after all local checks succeed.
        regression = self.suite(workspace, len(self.data['cycles']), stage='regression')
        if failure_count(regression['results']):
            raise Halt('Complete regression failed; repairs not accepted')
        if not baseline_failed <= {r['id'] for r in regression['results'] if r['status'] == 'passed'}:
            raise Intervention('Repaired targets missing or skipped in complete regression')
        return regression

    def validate_diff(self, workspace, base):
        names = git(workspace, 'diff', '--name-only', base).splitlines()
        untracked = git(workspace, 'ls-files', '--others', '--exclude-standard').splitlines()
        if untracked:
            raise Intervention('Worker created unreviewed files; retained in worktree for intervention')
        for name in names:
            if not self.allowed_repair_path(name):
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
            def stable():
                if (original is not None and original != source_fingerprint(ROOT)) or source_manifest(ROOT) != manifest:
                    raise Intervention('Original checkout changed; stop before retest/apply')

            baseline = self.suite(workspace, 0, stage='baseline')
            if failure_count(baseline['results']):
                if self.args.report_only:
                    self.data.update(status='failed', stop_reason='Report-only run; repairs disabled')
                    return 1
                self.repair_groups(workspace, baseline, base, stable)
            elif self.args.demo:
                raise Intervention('Demo seed did not produce the required failing regression')
            if base:
                patch = self.validate_diff(workspace, base)
                (self.report / 'repair.patch').write_text(sanitize(patch) + '\n')
                stable()
                if patch and not self.args.demo:
                    ensure_writer(ROOT)
                    check = subprocess.run(['git', 'apply', '--check', '-'], cwd=ROOT, input=patch+'\n', text=True, capture_output=True)
                    if check.returncode:
                        raise Intervention('Patch conflicts with original checkout; no changes applied')
                    subprocess.run(['git', 'apply', '-'], cwd=ROOT, input=patch+'\n', text=True, check=True)
                    manifest = source_manifest(ROOT)
                    self.data['repair_applied'] = True
            if source_manifest(ROOT) != manifest:
                raise Intervention('Source changed during validation; reports describe the frozen snapshot, rerun for the current source')
            for group in self.data['failure_groups']:
                group['result'] = 'accepted'
            for repair in self.data['repairs']:
                if repair['result'] == 'focused_and_subsystem_passed':
                    repair['result'] = 'accepted'
            self.data.update(status='passed', stop_reason='All executed checks passed')
            return 0
        finally:
            for repair in self.data['repairs']:
                if repair['result'] in {'awaiting_validation', 'focused_and_subsystem_passed'}:
                    repair['result'] = 'not_accepted'
            for group in self.data['failure_groups']:
                if group.get('result') == 'pending_regression':
                    group['result'] = 'not_accepted'
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
    parser.add_argument('--max-worker-tokens', type=int, default=30000)
    parser.add_argument('--worker-reasoning', choices=('medium', 'high'), default='medium', help='Initial reasoning; second attempt always uses high')
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
