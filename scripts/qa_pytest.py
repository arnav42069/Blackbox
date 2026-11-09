"""Opt-in pytest plugin: capture only isolated test app evidence, before teardown."""
import os
import re
from pathlib import Path
import pytest
from scripts.qa_support import write_json


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    destination = os.environ.get('BLACKBOX_QA_EVIDENCE')
    app = item.funcargs.get('app')
    if destination and app:
        name = re.sub(r'[^\w.-]', '_', item.nodeid)
        db = app.state.db
        write_json(Path(destination) / (name + '.json'), {
            'test': item.nodeid, 'source': 'isolated_test_application',
            'tools': db.rows('SELECT * FROM tool_events'),
            'events': db.rows('SELECT type,payload_json FROM event_journal'),
        })


def pytest_make_parametrize_id(config, val, argname):
    # Image limit tests pass multi-MiB bytes; pytest's default embeds every byte in JUnit.
    if isinstance(val, bytes) and len(val) > 128:
        import hashlib
        return f'bytes-{len(val)}-{hashlib.sha256(val).hexdigest()[:12]}'
    return None
