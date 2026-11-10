import base64
import io
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from PIL import Image

from blackbox.app import create_app
from blackbox.settings import Settings


class MockOllama:
    def __init__(self):
        self.requests = []
        self.review_requests = []
        self.vision_requests = []
        self.image_observation = {"category":"other", "visible_text":"red square", "uncertainties":[]}
        self.review_replies = []
        self.replies = []
        self.vision = True
        self.tools = True
        self.unavailable = False

    def __call__(self, request):
        if self.unavailable:
            raise httpx.ConnectError('fixture offline', request=request)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'fixture:4b', 'digest': 'fixture-digest', 'size': 100}]})
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['completion'] + (['vision'] if self.vision else []) + (['tools'] if self.tools else [])})
        body = json.loads(request.content)
        if body['messages'][0].get('content', '').startswith('BLACKBOX_IMAGE_OBSERVATIONS:'):
            self.vision_requests.append(body)
            return httpx.Response(200, json={'message': {'content':json.dumps(self.image_observation)}})
        if body.get('tools', [{}])[0].get('function', {}).get('name') == 'agent.review_completion':
            self.review_requests.append(body)
            verdict = self.review_replies.pop(0) if self.review_replies else {'decision': 'complete', 'reason': 'The requested fixture task has been completed with observed results.', 'remaining_steps': []}
            if 'tool_calls' in verdict or 'content' in verdict:
                message = verdict
            else:
                message = call('agent.review_completion', **verdict)
            return httpx.Response(200, json={'message': message, 'eval_count': 3})
        self.requests.append(body)
        if self.replies:
            return httpx.Response(200, json={'message': self.replies.pop(0), 'eval_count': 7})
        messages = body['messages']
        if any(m.get('images') for m in messages) and not body.get('tools'):
            content = 'red square'
        else:
            content = 'Fixture model answer'
        return httpx.Response(200, json={'message': {'role': 'assistant', 'content': content}, 'eval_count': 7})


@pytest.fixture
def mock_ollama():
    return MockOllama()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path/'data', config_dir=tmp_path/'config')


@pytest_asyncio.fixture
async def app(settings, mock_ollama):
    app = create_app(settings, httpx.MockTransport(mock_ollama))
    yield app
    await app.state.agent.shutdown()
    await app.state.ollama.close()
    app.state.db.close()


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765', headers={'Origin': 'http://127.0.0.1:8765'}) as client:
        r = await client.post('/api/pair', json={'token': app.state.auth.pairing})
        assert r.status_code == 200
        client.headers['Authorization'] = 'Bearer ' + r.json()['token']
        yield client


@pytest.fixture
def png():
    b = io.BytesIO()
    Image.new('RGB', (128, 64), 'red').save(b, format='PNG')
    return b.getvalue()


def call(name, **args):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': name, 'arguments': args}}]}
