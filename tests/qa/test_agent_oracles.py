"""Grounded localhost fixture task: a fresh nonce must travel through the real executor."""
import json
import secrets
from conftest import call
from scripts.qa_support import agent_findings


async def test_agent_delivers_independently_planted_observation(client, app, mock_ollama):
    sid = (await client.post('/api/sessions', json={'model':'fixture:4b'})).json()['id']
    nonce = 'QA-' + secrets.token_hex(12)
    (app.state.settings.data_dir / 'workspace' / 'observation.txt').write_text(nonce)
    # The model fixture cannot know the nonce until it actually receives the tool result.
    original = mock_ollama.__class__.__call__
    mock_ollama.replies = [call('workspace.read', path='observation.txt')]
    transport = app.state.ollama.http._transport
    def grounded(request):
        if request.url.path == '/api/chat':
            body = json.loads(request.content)
            if body.get('tools', [{}])[0].get('function', {}).get('name') != 'agent.review_completion':
                observations = [m for m in body['messages'] if m['role'] == 'tool']
                if observations:
                    actual = json.loads(observations[-1]['content'])['stdout']
                    mock_ollama.replies.append({'content': 'Observed: ' + actual})
        return original(mock_ollama, request)
    transport.handler = grounded
    response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Read observation.txt from the workspace and return its exact contents.'})
    assert response.status_code == 200
    await app.state.agent.tasks[sid]
    assert app.state.db.session(sid)['status'] == 'completed'
    tools = app.state.db.rows('SELECT * FROM tool_events')
    answer = app.state.db.messages(sid)[-1]['content']
    assert not agent_findings(app.state.db.events(sid), tools, required_tools=['workspace.read'], expected_text=nonce, final_text=answer, terminal='completed')
    assert nonce not in mock_ollama.requests[0]['messages'][1]['content']
