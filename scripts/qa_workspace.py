#!/usr/bin/env python3
"""Run the deterministic workspace benchmark and related defensive checks."""
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ('tests/integration/test_tool_recovery.py::'
             'test_local_workspace_workflow_recovers_and_reports_verified_content')
COUNT_METRICS = (
    'tool_outcomes', 'successful_executions', 'executions', 'redundant_executions',
    'duplicate_proposals_suppressed', 'recovered_validation_errors', 'recovered_transient_failures',
)
BACKEND_TESTS = [
    'tests/integration/test_local_completion.py',
    'tests/integration/test_tool_recovery.py::test_safe_workspace_path_and_integer_repairs',
    'tests/integration/test_tool_recovery.py::test_ambiguous_or_escaping_paths_are_never_guessed',
    'tests/integration/test_tool_recovery.py::test_symlink_escape_is_denied_without_opening_secret',
    'tests/integration/test_tool_recovery.py::test_symlink_swap_between_validation_and_open_is_confined',
    'tests/integration/test_tool_recovery.py::test_no_unbounded_recovery_when_model_ignores_errors',
    'tests/integration/test_tools.py::test_inspection_url_rejects_ambiguous_authority',
    'tests/integration/test_tools.py::test_http_inspection_ignores_valueless_metadata',
    'tests/integration/test_tools.py::test_actual_http_pinned_redirect_and_budget',
    'tests/integration/test_tools.py::test_explicit_http_inspection_redirect_policy',
    'tests/integration/test_tools.py::test_explicit_http_get_redirect_limit',
    'tests/integration/test_tools.py::test_explicit_http_inspection_requires_current_scope',
    'tests/integration/test_conversation_streaming.py::test_budget_exhaustion_visible_checkpoint',
    'tests/integration/test_conversation_streaming.py::test_empty_model_response_visible_error',
    'tests/integration/test_conversation_streaming.py::test_normal_conversation_no_tools',
    'tests/integration/test_conversation_streaming.py::test_restart_preserves_partial_and_persists_interruption',
    'tests/unit/test_storage.py::test_restart_preserves_history_revokes_scope_and_interrupts_tools',
    BENCHMARK,
]


def read_benchmark_metrics(report):
    cases = [case for case in ElementTree.parse(report).iter('testcase')
             if case.find(f"properties/property[@name='qa_nodeid'][@value='{BENCHMARK}']") is not None]
    if not cases:
        raise ValueError('required workspace benchmark is missing from JUnit (possibly deselected)')
    if len(cases) != 1:
        raise ValueError('JUnit contains multiple results for the workspace benchmark')
    case, = cases
    for outcome in ('skipped', 'failure', 'error'):
        if case.find(outcome) is not None:
            raise ValueError(f'workspace benchmark did not pass ({outcome})')

    properties = {item.get('name'): item.get('value') for item in case.findall('properties/property')}
    names = ('objective_completion_rate', *COUNT_METRICS)
    missing = [name for name in names if not properties.get(name)]
    if missing:
        raise ValueError('workspace benchmark is missing metrics: ' + ', '.join(missing))
    if properties['objective_completion_rate'] != '1/1 (100%)':
        raise ValueError('workspace benchmark did not report full objective completion')
    for name in COUNT_METRICS:
        value = properties[name]
        if not (value.isascii() and value.isdecimal()):
            raise ValueError(f'workspace benchmark metric {name} is not a nonnegative integer')
    if not 0 < int(properties['successful_executions']) <= int(properties['executions']) <= int(properties['tool_outcomes']):
        raise ValueError('workspace benchmark did not report consistent execution counts')
    return {name: properties[name] for name in names}


def main():
    report = Path(tempfile.mkdtemp(prefix='blackbox-qa-workspace-'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        'PYTHONDONTWRITEBYTECODE': '1',
        'BLACKBOX_QA_EVIDENCE': str(report / 'evidence'),
        'BLACKBOX_QA_REPORT': str(report),
        'BLACKBOX_QA_PYTHON': sys.executable,
        'BLACKBOX_E2E_PORT': str(port),
        'BLACKBOX_E2E_RUNTIME': str(report / 'runtime'),
        'BLACKBOX_E2E_FRONTEND_DIR': str(report / 'frontend'),
    }
    backend_report = report / 'backend.xml'
    npm = ['npm', '--prefix', 'frontend']
    browser_tests = ('workspace tool arguments|journal reconnect|Stop cancels|Retry starts|delayed send|'
                     'session snapshots fail|restored missing model|restored session|clipboard|screenshot-only|capability probe')
    print('Workspace QA artifacts:', report, flush=True)
    def run(command):
        print('Running:', shlex.join(command), flush=True)
        return subprocess.run(command, cwd=ROOT, env=env).returncode

    backend_status = run([
        sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'scripts.qa_pytest',
        '-o', 'junit_family=legacy', f'--basetemp={report / "pytest"}', f'--junitxml={backend_report}',
        *BACKEND_TESTS,
    ])
    if backend_status:
        return backend_status
    try:
        metrics = read_benchmark_metrics(backend_report)
    except (OSError, ElementTree.ParseError, ValueError) as exc:
        print(f'Workspace QA failed: {exc} (report: {backend_report})', file=sys.stderr)
        return 1
    print('Workspace benchmark metrics:')
    for key, value in metrics.items():
        print(f'  {key}: {value}')

    commands = [
        [*npm, 'test'],
        [*npm, 'run', 'build', '--', '--outDir', str(report / 'frontend')],
        [*npm, 'run', 'test:e2e', '--', 'chat-tool-loop.spec.ts', 'model-selection.spec.ts', 'paste-image.spec.ts', '-g', browser_tests],
    ]
    for command in commands:
        status = run(command)
        if status:
            return status
    print('Workspace QA passed. Reports:', report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
