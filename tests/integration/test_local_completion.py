import pytest

from conftest import call
from test_api import finish, new


@pytest.mark.parametrize(('paths', 'expected'), [
    (['Plans and read notes.txt', './other.txt'], 'completed'),
    (['Plans and read notes.txt', './Plans and read notes.txt'], 'checkpointed'),
    (['other.txt'], 'checkpointed'),
])
async def test_named_workspace_reads_require_distinct_complete_evidence(client, app, mock_ollama, paths, expected):
    sid = await new(client)
    root = app.state.settings.data_dir / 'workspace'
    for name in ('Plans and read notes.txt', 'other.txt'):
        (root / name).write_text(f'Synthetic document for {name}')
    app.state.settings.prefs.max_turns = len(paths) + 2
    mock_ollama.replies = [call('workspace.read', path=path) for path in paths] + [{'content': 'Both files read.'}]
    response = await client.post(
        f'/api/sessions/{sid}/messages',
        data={'text': 'Read workspace file "Plans and read notes.txt" and read workspace file other.txt'},
    )
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == expected
    events = app.state.db.events(sid)
    expected_executions = 2 if expected == 'completed' else 1
    assert len([event for event in events if event['type'] == 'tool_started']) == expected_executions
    assert len([event for event in events if event['type'] == 'tool_repeat_suppressed']) == len(paths) - expected_executions


async def test_named_workspace_read_without_tools_cannot_claim_completion(client, app, mock_ollama):
    sid = await new(client)
    (app.state.settings.data_dir / 'workspace' / 'required.txt').write_text('Synthetic document.')
    mock_ollama.tools = False
    mock_ollama.replies = [{'content': 'I read the required file.'}]
    response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Read workspace file required.txt'})
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == 'needs_access'
    final = app.state.db.messages(sid)[-1]['content']
    assert final.startswith('Unverified partial response:')
    assert 'Verified tool capability is unavailable' in final
    assert not app.state.db.rows('SELECT id FROM tool_events')


async def test_truncated_workspace_read_is_partial(client, app, mock_ollama):
    sid = await new(client)
    (app.state.settings.data_dir / 'workspace' / 'required.txt').write_text('Synthetic document.')
    app.state.settings.prefs.max_turns = 3
    mock_ollama.replies = [call('workspace.read', path='required.txt', max_bytes=1), {'content': 'Fully read.'}]
    response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Read workspace file required.txt'})
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == 'checkpointed'


@pytest.mark.parametrize(('attachment', 'expected'), [('current', 'completed'), ('prior', 'checkpointed'), ('absent', 'checkpointed')])
async def test_image_and_host_require_current_attachment_and_host_result(client, app, mock_ollama, png, attachment, expected):
    sid = await new(client)
    endpoint = f'/api/sessions/{sid}/messages'
    image = {'images': ('screen.png', png, 'image/png')}
    if attachment == 'prior':
        assert (await client.post(endpoint, data={'text': 'Read this screenshot'}, files=image)).status_code == 200
        assert (await finish(app, sid))['status'] == 'completed'
    app.state.settings.prefs.max_turns = 3
    mock_ollama.replies = [call('host.preflight'), {'content': 'Observed a red square and checked this host.'}]
    response = await client.post(endpoint, data={'text': 'Describe image and check host'},
                                 files=image if attachment == 'current' else None)
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == expected
    run = response.json()['run_id']
    events = [event for event in app.state.db.events(sid) if event['run_id'] == run]
    if expected == 'completed':
        visual, = [event for event in events if event['type'] == 'image_observations']
        host, = [event for event in events if event['type'] == 'tool_finished']
        assert visual['evidence_id'] != host['tool_id']
        final = app.state.db.messages(sid)[-1]['content']
        assert all(evidence_id in final for evidence_id in (visual['evidence_id'], host['tool_id']))
    else:
        assert not any(e.get('decision') == 'complete' for e in events if e['type'] == 'completion_review')


async def test_local_image_request_cannot_execute_unrelated_tool(client, app, mock_ollama, png):
    sid = await new(client)
    mock_ollama.replies = [call('workspace.list')]
    response = await client.post(
        f'/api/sessions/{sid}/messages', data={'text': 'Describe image and check host'},
        files={'images': ('screen.png', png, 'image/png')},
    )
    assert response.status_code == 200
    assert (await finish(app, sid))['status'] == 'failed'
    assert not app.state.db.rows('SELECT id FROM tool_events')
