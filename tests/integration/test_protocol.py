"""Malformed JSON response shapes must use the bounded protocol repair path."""
import pytest
from conftest import call


@pytest.mark.parametrize('malformed', [None, [], 'invalid', 7])
async def test_malformed_message_shape_gets_one_repair(client, app, mock_ollama, malformed):
    sid = (await client.post('/api/sessions', json={'model': 'fixture:4b'})).json()['id']
    mock_ollama.replies = [malformed, call('host.preflight'), {'content': 'Recovered with real host evidence.'}]
    response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Check host preflight.'})
    assert response.status_code == 200
    await app.state.agent.tasks[sid]
    assert app.state.db.session(sid)['status'] == 'completed'
    records = app.state.db.rows('SELECT tool_name,status FROM tool_events')
    assert records == [{'tool_name': 'host.preflight', 'status': 'success'}]
    assert len(mock_ollama.requests) == 3


@pytest.mark.parametrize('malformed', [None, [], 'invalid', 7])
async def test_repeated_malformed_review_is_reported(client, app, mock_ollama, malformed):
    sid = (await client.post('/api/sessions', json={'model': 'fixture:4b'})).json()['id']
    mock_ollama.replies = [call('host.preflight'), {'content': 'Host checked.'}]
    # This hook supplies invalid message shapes without fixture-side dict assumptions.
    original = app.state.ollama.chat
    reviews = []
    async def chat(model, messages, tools=None):
        if tools and tools[0]['function']['name'] == 'agent.review_completion':
            reviews.append(True)
            return {'message': malformed}
        return await original(model, messages, tools)
    app.state.ollama.chat = chat
    await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Check host preflight.'})
    await app.state.agent.tasks[sid]
    assert app.state.db.session(sid)['status'] == 'failed'
    assert len(reviews) == 2
    assert any('invalid completion review twice' in event.get('message', '') for event in app.state.db.events(sid))
