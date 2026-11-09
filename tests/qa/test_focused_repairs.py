"""Focused routing, bounded retries, immutable oracles and measured worker usage."""
import argparse
import json
from pathlib import Path
import sys

import pytest

import scripts.qa_loop as controller
from scripts.qa_focus import concise_failures, failure_excerpts, failure_groups, relevant_sources, token_usage
from scripts.qa_loop import Halt, Intervention, Loop, git, junit_results, playwright_results


def result(node='tests/unit/test_context.py::test_value', status='failed', category='regression'):
    return {'id': node, 'status': status, 'category': category, 'detail': 'assert 0 == 1'}


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, 'ROOT', tmp_path)
    monkeypatch.setattr(controller, 'ensure_writer', lambda *args: None)
    args = argparse.Namespace(report_only=False, demo=False, budget_seconds=60, max_cycles=5,
                              max_worker_tokens=30000, worker_reasoning='medium', worker_seconds=10,
                              max_report_mb=64, max_log_kb=1024, test_port=8876, model=None)
    instance = Loop(args)
    (tmp_path / 'backend/blackbox/core').mkdir(parents=True)
    (tmp_path / 'backend/blackbox/core/context.py').write_text('value = 0\n')
    (tmp_path / 'tests/unit').mkdir(parents=True)
    (tmp_path / 'tests/unit/test_context.py').write_text('from blackbox.core.context import value\ndef test_value(): assert value == 1\n')
    (tmp_path / '.gitignore').write_text('.runtime/\nqa-reports/\n')
    git(tmp_path, 'init', '-b', 'main')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'fixture')
    return instance, tmp_path


def scripted_cycle(loop, monkeypatch, outcomes):
    instance, root = loop
    stages = []
    def suite(workspace, index, *, stage, targets=None):
        stages.append(stage)
        cycle = {'index': index, 'stage': stage, 'results': next(outcomes), 'targets': targets}
        instance.data['cycles'].append(cycle)
        return cycle
    monkeypatch.setattr(instance, 'suite', suite)
    def worker(workspace, group, number):
        source = root / 'backend/blackbox/core/context.py'
        source.write_text(f'value = {number}\n')
        instance.data['repairs'].append({'cycle': number, 'group': group['id'], 'attempt': group['attempts'],
                                        'reasoning': 'medium' if group['attempts'] == 1 else 'high',
                                        'result': 'awaiting_validation'})
    monkeypatch.setattr(instance, 'repair', worker)
    return stages


def test_exact_junit_class_and_parameter_nodeid(tmp_path):
    xml = tmp_path / 'report.xml'
    xml.write_text('<testsuites><testcase classname="tests.unit.test_thing.TestClass" name="test_x[a.b]">'
                   '<properties><property name="qa_nodeid" value="tests/unit/test_thing.py::TestClass::test_x[a.b]"/>'
                   '</properties><failure>failed</failure></testcase></testsuites>')
    assert junit_results(xml)[0]['id'] == 'tests/unit/test_thing.py::TestClass::test_x[a.b]'


def test_pytest_plugin_records_real_class_and_param_nodeids(tmp_path):
    test = tmp_path / 'test_nodes.py'
    test.write_text('import pytest\nclass TestClass:\n @pytest.mark.parametrize("x", ["a.b", "c"] )\n def test_x(self,x): assert x\n')
    import subprocess
    xml = tmp_path / 'report.xml'
    subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'scripts.qa_pytest',
                    f'--junitxml={xml}', str(test)], check=True, capture_output=True)
    assert [r['id'].split('::')[-2:] for r in junit_results(xml)] == [['TestClass', 'test_x[a.b]'], ['TestClass', 'test_x[c]']]


def test_grouping_and_source_hints(loop):
    instance, root = loop
    groups = failure_groups([result(), result('tests/unit/test_context.py::test_other'),
                             result('build'), result('typecheck'), result(status='skipped')])
    assert len(groups) == 2
    assert len(groups[0]['failures']) == 2
    assert relevant_sources(root, groups[0]['failures']) == ['backend/blackbox/core/context.py']
    assert instance.focused_targets(groups[0])['pytest'] == [r['id'] for r in groups[0]['failures']]
    assert instance.subsystem_targets(groups[0], ['backend/blackbox/core/context.py'])['pytest'] == [
        'tests/unit', 'tests/integration', 'tests/qa']


def test_browser_selector_preserves_title_project_and_parameter_line(tmp_path):
    report = tmp_path / 'browser.json'
    report.write_text(json.dumps({'suites': [{'specs': [{'file': 'paste.spec.ts', 'line': 8,
        'title': 'paste [image]', 'tests': [{'status': 'unexpected', 'projectName': 'chromium', 'results': []}]}]}]}))
    group = failure_groups(playwright_results(report))[0]
    assert group['failures'][0]['id'] == 'e2e:paste.spec.ts:8:paste [image]:chromium'
    assert Loop.focused_targets(group) == {'browser': [[r'paste\.spec\.ts:8', '--grep', r'paste\ \[image\]$', '--project', 'chromium']]}


@pytest.mark.parametrize('build_exit', [0, 1])
def test_focused_browser_rebuilds_and_never_uses_stale_output(loop, monkeypatch, build_exit):
    instance, root = loop
    artifact = root / 'frontend/dist/index.html'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('stale UI')
    class FreeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect_ex(self, address): return 111
    monkeypatch.setattr(controller.socket, 'socket', FreeSocket)
    calls = []
    def command(argv, cwd, output, **kwargs):
        calls.append(argv)
        output.write_text('build failed' if build_exit else 'ok')
        if argv[2] == 'build':
            if not build_exit:
                artifact.write_text('repaired UI')
            return build_exit
        assert artifact.read_text() == 'repaired UI'
        Path(kwargs['env']['PLAYWRIGHT_JSON_OUTPUT_FILE']).write_text(json.dumps({'suites': [{'specs': [
            {'file': 'paste.spec.ts', 'line': 8, 'title': 'paste', 'tests': [{'status': 'expected'}]}]}]}))
        return 0
    monkeypatch.setattr(instance, 'command', command)
    cycle = instance.suite(root, 0, stage='focused', targets={'browser': [['paste.spec.ts:8', '--grep', 'paste$']]})
    assert calls[0] == ['npm', 'run', 'build']
    assert len(calls) == (1 if build_exit else 2)
    assert cycle['results'][0]['status'] == ('failed' if build_exit else 'passed')
    assert cycle['results'][-1]['status'] == ('skipped' if build_exit else 'passed')


def test_context_is_bounded_and_redacted():
    failures = [dict(result(f'tests/test_x.py::test_{i}'), detail='x' * 12000 + ' password=fixture-secret') for i in range(100)]
    context = json.dumps(concise_failures(failures))
    assert len(context) < 14000
    assert 'fixture-secret' not in context


def test_source_hints_use_only_imports_referenced_by_failing_test(loop):
    _, root = loop
    path = root / 'tests/unit/test_context.py'
    path.write_text('from scripts.qa_loop import Loop\nfrom blackbox.core.context import value\n'
                    'def test_value(): assert value == 1\ndef test_unrelated(): return Loop\n')
    (root / 'scripts').mkdir()
    (root / 'scripts/qa_loop.py').write_text('class Loop: pass\n')
    assert relevant_sources(root, [result()]) == ['backend/blackbox/core/context.py']


def test_excerpts_include_exact_oracle_and_small_source_with_a_bound(loop):
    _, root = loop
    sources = ['backend/blackbox/core/context.py']
    excerpts = failure_excerpts(root, [result()], sources)
    assert excerpts[result()['id']] == 'def test_value(): assert value == 1'
    assert excerpts[sources[0]] == 'value = 0\n'
    assert failure_excerpts(root, [result()], sources, limit=1) == {}


def test_usage_does_not_double_count_cache_and_missing_is_unknown():
    assert token_usage([]) is None
    assert token_usage([{'type': 'turn.completed', 'usage': {}}]) is None
    usage = token_usage([{'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20}},
                         {'type': 'item.completed', 'usage': {'input_tokens': 999}}])
    assert usage == {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 20, 'total_tokens': 120}


def test_worker_prompt_reasoning_budget_and_usage(loop, monkeypatch):
    instance, root = loop
    group = failure_groups([result()])[0]
    group.update(attempts=1, source_files=['backend/blackbox/core/context.py'])
    calls = []
    def command(argv, cwd, output, **kwargs):
        calls.append((argv, kwargs['prompt']))
        path = root / 'backend/blackbox/core/context.py'
        instance.worker_edits = [{'path': 'backend/blackbox/core/context.py', 'old': path.read_text(),
                                 'new': f'value = {len(calls)}\n'}]
        instance.data['commands'].append({'token_usage': {'total_tokens': 123}})
        instance.data['worker_tokens'] += 123
        return 0
    monkeypatch.setattr(instance, 'command', command)
    instance.data['cycles'] = [{'index': 0, 'results': [dict(result(), detail='UNRELATED_QA_HISTORY')]}]
    instance.repair(root, group, 1)
    group['attempts'] = 2
    instance.repair(root, group, 2)
    assert 'model_reasoning_effort="medium"' in calls[0][0]
    assert 'model_reasoning_effort="high"' in calls[1][0]
    assert 'features.multi_agent=false' in calls[0][0]
    assert 'sandbox_workspace_write.network_access=false' in calls[0][0]
    assert 'approval_policy="never"' in calls[0][0]
    assert '--ignore-user-config' in calls[0][0]
    assert '--output-schema' in calls[0][0]
    assert 'UNRELATED_QA_HISTORY' not in calls[0][1]
    assert '30000 input+output' in calls[0][1]
    assert '29877 input+output' in calls[1][1]
    assert instance.data['repairs'][0]['tokens'] == 123
    assert instance.data['repairs'][0]['elapsed_seconds'] >= 0


@pytest.mark.parametrize('usage,code,expected', [(None, 0, Intervention), ({'total_tokens': 30001}, 0, Halt),
                                               ({'total_tokens': 10}, 1, Intervention)])
def test_missing_usage_over_budget_and_worker_failure_rejected(loop, monkeypatch, usage, code, expected):
    instance, root = loop
    group = failure_groups([result()])[0]
    group.update(attempts=1, source_files=[])
    def command(*args, **kwargs):
        instance.data['commands'].append({'token_usage': usage})
        instance.data['worker_tokens'] += usage['total_tokens'] if usage else 0
        return code
    monkeypatch.setattr(instance, 'command', command)
    with pytest.raises(expected):
        instance.repair(root, group, 1)
    assert instance.data['repairs'][0]['result'] == 'worker_failed_or_budget_stopped'


def test_budget_prevents_starting_another_worker(loop, monkeypatch):
    instance, root = loop
    instance.data['worker_tokens'] = 30000
    group = failure_groups([result()])[0]
    group.update(attempts=1, source_files=[])
    monkeypatch.setattr(instance, 'command', lambda *a, **kw: pytest.fail('must not invoke Codex'))
    with pytest.raises(Halt, match='token budget'):
        instance.repair(root, group, 1)


def test_command_retains_actual_usage_and_discards_private_stdout(loop):
    instance, root = loop
    log = instance.report / 'worker.json'
    events = [{'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'PRIVATE_REASONING'}},
              {'type': 'turn.completed', 'usage': {'input_tokens': 210, 'cached_input_tokens': 100, 'output_tokens': 30}}]
    code = 'import json; print("PRIVATE_STDOUT"); print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')'
    assert instance.command([sys.executable, '-c', code], root, log, worker=True) == 0
    saved = log.read_text()
    assert 'PRIVATE_STDOUT' not in saved and 'PRIVATE_REASONING' not in saved
    assert instance.data['worker_tokens'] == 240
    assert instance.data['commands'][-1]['token_usage']['cached_input_tokens'] == 100
    assert instance.data['commands'][-1]['elapsed_seconds'] >= 0


def test_interrupted_worker_usage_is_incomplete_even_when_some_turns_completed(loop):
    instance, root = loop
    code = 'import json,sys; print(json.dumps({"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}})); sys.exit(1)'
    assert instance.command([sys.executable, '-c', code], root, instance.report / 'worker.json', worker=True) == 1
    assert instance.data['worker_tokens'] == 7
    assert instance.data['worker_usage_complete'] is False


def test_structured_proposal_is_parsed_without_retaining_message_text(loop):
    instance, root = loop
    log = instance.report / 'worker.json'
    proposal = {'edits': [{'path': 'backend/blackbox/core/context.py', 'old': 'value = 0', 'new': 'value = 1'}]}
    event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(proposal)}}
    code = 'print(' + repr(json.dumps(event)) + ')'
    instance.command([sys.executable, '-c', code], root, log, worker=True)
    assert instance.worker_edits == proposal['edits']
    assert 'value = 1' not in log.read_text()
    assert instance.data['worker_usage_complete'] is False


def test_proposal_applies_exact_replacements_without_changing_modes(loop):
    instance, root = loop
    path = root / 'backend/blackbox/core/context.py'
    mode = path.stat().st_mode
    assert instance.apply_worker_edits(root, [{'path': 'backend/blackbox/core/context.py',
                                             'old': 'value = 0', 'new': 'value = 1'}]) == 1
    assert path.read_text() == 'value = 1\n'
    assert path.stat().st_mode == mode


@pytest.mark.parametrize('edit', [
    {'path': 'tests/unit/test_context.py', 'old': 'assert value == 1', 'new': 'assert True'},
    {'path': 'backend/blackbox/./core/policy.py', 'old': 'x', 'new': 'y'},
    {'path': 'backend/blackbox/core/../core/context.py', 'old': 'value = 0', 'new': 'value = 2'},
    {'path': 'backend/blackbox/core/new.py', 'old': 'x', 'new': 'y'},
    {'path': 'backend/blackbox/core/context.py', 'old': 'absent', 'new': 'value = 2'},
    {'path': 'backend/blackbox/core/context.py', 'old': '', 'new': 'value = 2'},
    {'path': 'backend/blackbox/core/context.py', 'old': 'value = 0', 'new': 'value = 0'},
    {'path': 'backend/blackbox/core/context.py', 'old': 'value = 0', 'new': 2},
])
def test_invalid_proposal_does_not_partially_apply(loop, edit):
    instance, root = loop
    path = root / 'backend/blackbox/core/context.py'
    first = {'path': 'backend/blackbox/core/context.py', 'old': 'value = 0', 'new': 'value = 1'}
    with pytest.raises(Intervention):
        instance.apply_worker_edits(root, [first, edit])
    assert path.read_text() == 'value = 0\n'


def test_retry_then_subsystem_then_one_full_regression(loop, monkeypatch):
    instance, root = loop
    stages = scripted_cycle(loop, monkeypatch, iter([[result()], [result()], [result(status='passed')],
                                                    [result(status='passed')], [result(status='passed')]]))
    instance.repair_groups(root, {'results': [result()]}, 'HEAD', lambda: None)
    assert stages == ['reproduce', 'focused', 'focused', 'subsystem', 'regression']
    assert [r['reasoning'] for r in instance.data['repairs']] == ['medium', 'high']
    assert instance.data['repairs'][0]['result'] == 'failure_persists'


@pytest.mark.parametrize('max_cycles,worker_count', [(5, 2), (1, 1), (0, 0)])
def test_attempt_limits_and_once_stop_without_full_suite(loop, monkeypatch, max_cycles, worker_count):
    instance, root = loop
    instance.args.max_cycles = max_cycles
    stages = scripted_cycle(loop, monkeypatch, iter([[result()]] * 4))
    with pytest.raises(Halt):
        instance.repair_groups(root, {'results': [result()]}, 'HEAD', lambda: None)
    assert len(instance.data['repairs']) == worker_count
    assert 'regression' not in stages


@pytest.mark.parametrize('outcomes,message', [([[result(status='passed')]], 'reproduction differs'),
    ([[result()], [result(status='skipped')]], 'missing or skipped'),
    ([[result()], [result(status='passed')], [result('tests/unit/test_other.py::test_new')]], 'new regression'),
    ([[result()], [result(status='passed')], [result(status='passed')], [result()]], 'Complete regression'),
    ([[result()], [result(status='passed')], [result(status='passed')], [result(status='skipped')]], 'missing or skipped in complete')])
def test_unreliable_skipped_new_and_final_failures_rejected(loop, monkeypatch, outcomes, message):
    instance, root = loop
    scripted_cycle(loop, monkeypatch, iter(outcomes))
    with pytest.raises((Halt, Intervention), match=message):
        instance.repair_groups(root, {'results': [result()]}, 'HEAD', lambda: None)


def test_known_other_group_failure_is_not_a_new_regression(loop, monkeypatch):
    instance, root = loop
    other = result('tests/unit/test_other.py::test_other')
    stages = scripted_cycle(loop, monkeypatch, iter([[result()], [result(status='passed')],
        [result(status='passed'), other], [dict(other, status='passed')], [result(status='passed'), dict(other, status='passed')]]))
    instance.repair_groups(root, {'results': [result(), other]}, 'HEAD', lambda: None)
    assert stages == ['reproduce', 'focused', 'subsystem', 'reproduce', 'regression']
    assert len(instance.data['repairs']) == 1


@pytest.mark.parametrize('category', ['infrastructure', 'scope_enforcement', 'model_capability'])
def test_intervention_failures_never_invoke_worker(loop, monkeypatch, category):
    instance, root = loop
    monkeypatch.setattr(instance, 'repair', lambda *a: pytest.fail('protected failure'))
    with pytest.raises(Intervention):
        instance.repair_groups(root, {'results': [result(category=category)]}, 'HEAD', lambda: None)
    assert not instance.data['repairs']


@pytest.mark.parametrize('name', ['tests/unit/test_context.py', 'backend/blackbox/core/policy.py',
    'backend/blackbox/core/completion.py', 'frontend/src/components/Messages.tsx', 'scripts/qa_pytest.py'])
def test_protected_oracle_and_security_files_rejected(loop, name):
    instance, root = loop
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('original\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '--allow-empty', '-m', 'control')
    path.write_text('weakened\n')
    with pytest.raises(Intervention, match='protected'):
        instance.validate_diff(root, 'HEAD')


def test_git_commit_by_worker_is_rejected_before_retest(loop, monkeypatch):
    instance, root = loop
    stages = scripted_cycle(loop, monkeypatch, iter([[result()]]))
    original_worker = instance.repair
    def worker(*args):
        original_worker(*args)
        git(root, 'add', '.')
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'forbidden')
    monkeypatch.setattr(instance, 'repair', worker)
    with pytest.raises(Intervention, match='Git history or index'):
        instance.repair_groups(root, {'results': [result()]}, 'HEAD', lambda: None)
    assert stages == ['reproduce']
