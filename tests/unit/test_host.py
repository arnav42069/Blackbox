import pytest
from pydantic import ValidationError

from blackbox.tools.host import Diagnostics, snapshot, diagnostics
from blackbox.tools.base import ToolResult


def test_hardware_is_parsed_from_measurements_not_prd_defaults():
    data = snapshot({'uname': 'TestLinux measured-kernel testarch\n',
                     'cat': 'PRETTY_NAME="Measured OS"\nID=testos\n',
                     'free': 'total used free shared buff/cache available\nMem: 123456 200 300 400 500 65432\n',
                     'lscpu': '{"lscpu":[{"field":"CPU(s):","data":"3"},{"field":"Model name:","data":"Measured CPU"}]}',
                     'nvidia-smi': 'Measured GPU, 123, driver-test\n'})
    assert data['kernel'] == 'measured-kernel'
    assert data['distribution']['PRETTY_NAME'] == 'Measured OS'
    assert data['memory'] == {'total_bytes': 123456, 'available_bytes': 65432}
    assert data['cpu']['model'] == 'Measured CPU' and data['cpu']['logical_cpus'] == 3
    assert data['gpus'] == [{'name': 'Measured GPU', 'memory_total_mib': 123, 'driver_version': 'driver-test'}]


def test_unknown_hardware_is_never_filled_with_defaults():
    data = snapshot({})
    for key in ('os', 'kernel', 'distribution', 'architecture', 'memory', 'cpu', 'gpus'):
        assert data[key] is None
    assert snapshot({'lscpu': 'invalid json', 'nvidia-smi': 'driver failed', 'free': 'invalid'})['cpu'] is None


@pytest.mark.parametrize('args', [{'sections': ['bash']}, {'command': 'uname -a'}, {'sections': ['gpu'], 'flags': ['--foo']}])
def test_diagnostics_cannot_become_shell_or_arbitrary_command(args):
    with pytest.raises(ValidationError):
        Diagnostics.model_validate(args)


async def test_missing_and_failed_commands_are_unknown_and_recorded():
    from types import SimpleNamespace
    class MissingRunner:
        binaries = {}
        async def run(self, binary, args, **kw):
            return ToolResult(ok=False, exit_code=1, stdout='FAKE RTX 9999', summary='Unavailable command')
    result = await diagnostics(SimpleNamespace(runner=MissingRunner(), command=lambda argv: None), Diagnostics(sections=['gpu']))
    assert not result.ok and result.evidence[0]['gpus'] is None
    assert all(not r['ok'] for r in result.evidence[1:])
