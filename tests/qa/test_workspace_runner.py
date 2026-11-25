from xml.etree import ElementTree

import pytest

from scripts.qa_workspace import BENCHMARK, read_benchmark_metrics


METRICS = {
    'objective_completion_rate': '1/1 (100%)',
    'tool_outcomes': '5',
    'successful_executions': '2',
    'executions': '3',
    'redundant_executions': '0',
    'duplicate_proposals_suppressed': '1',
    'recovered_validation_errors': '1',
    'recovered_transient_failures': '1',
}


@pytest.fixture
def junit(tmp_path):
    def write(nodeid=BENCHMARK, outcome=None, metrics=METRICS):
        suite = ElementTree.Element('testsuite')
        case = ElementTree.SubElement(suite, 'testcase')
        properties = ElementTree.SubElement(case, 'properties')
        for name, value in {'qa_nodeid': nodeid, **metrics}.items():
            ElementTree.SubElement(properties, 'property', name=name, value=value)
        if outcome:
            ElementTree.SubElement(case, outcome)
        report = tmp_path / 'backend.xml'
        ElementTree.ElementTree(suite).write(report)
        return report
    return write


def test_workspace_report_returns_measured_metrics(junit):
    assert read_benchmark_metrics(junit()) == METRICS


def test_other_passing_cases_do_not_satisfy_workspace_benchmark(junit):
    with pytest.raises(ValueError, match='benchmark is missing'):
        read_benchmark_metrics(junit(nodeid='tests/test_unrelated.py::test_passes'))


@pytest.mark.parametrize('outcome', ['skipped', 'failure', 'error'])
def test_nonpassing_workspace_benchmark_is_rejected_even_with_metrics(junit, outcome):
    with pytest.raises(ValueError, match=rf'did not pass \({outcome}\)'):
        read_benchmark_metrics(junit(outcome=outcome))


@pytest.mark.parametrize(('metrics', 'error'), [
    ({key: value for key, value in METRICS.items() if key != 'executions'}, 'missing metrics: executions'),
    ({**METRICS, 'objective_completion_rate': '0/1 (0%)'}, 'did not report full objective completion'),
    ({**METRICS, 'executions': 'unknown'}, 'executions is not a nonnegative integer'),
    ({**METRICS, 'executions': '0'}, 'did not report consistent execution counts'),
])
def test_workspace_benchmark_requires_complete_measurements(junit, metrics, error):
    with pytest.raises(ValueError, match=error):
        read_benchmark_metrics(junit(metrics=metrics))
