import base64
import json
from pathlib import Path

import httpx
import pytest

from blackbox.core.ollama_client import OllamaClient
from blackbox.settings import Settings
from blackbox.storage.db import Database


@pytest.mark.parametrize('name',['abliterated:test','uncensored:test','plain:test'])
@pytest.mark.parametrize('capabilities', [['vision'],['tools'],['vision','tools'],[]])
async def test_metadata_and_independent_probes(tmp_path,capabilities,name):
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config')
    db=Database(tmp_path/'test.db',recover=False)
    seen=[]
    digest=['first']
    def respond(request):
        if request.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':name,'digest':digest[0]}]})
        if request.url.path=='/api/show': return httpx.Response(200,json={'capabilities':capabilities})
        body=json.loads(request.content);seen.append(body)
        if body.get('tools'):
            value='blackbox-second' if len(body['messages'])>1 else 'blackbox-probe'
            return httpx.Response(200,json={'message':{'content':'', 'tool_calls':[{'function':{'name':'echo_test','arguments':{'value':value}}}]}})
        if len(body['messages'])>1:
            return httpx.Response(200,json={'message':{'content':'blackbox-probe-complete'}})
        image=base64.b64decode(body['messages'][0]['images'][0]);assert image.startswith(b'\x89PNG\r\n\x1a\n')
        return httpx.Response(200,json={'message':{'content':'red square'}})
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        before=await client.capabilities(name)
        assert before['vision_advertised']==('vision' in capabilities)
        assert before['tools_advertised']==('tools' in capabilities)
        assert before['vision'] is False and before['tools'] is False
        outcome=await client.probe(name)
        assert outcome['vision']==('vision' in capabilities)
        assert outcome['tools']==('tools' in capabilities)
        assert len(seen)==len(capabilities)+(2 if 'tools' in capabilities else 0)
        after=await client.capabilities(name)
        assert after['vision']==outcome['vision'] and after['tools']==outcome['tools']
        digest[0]='second';changed=(await client.models(refresh=True))[0]
        assert changed['digest']=='second' and not changed['vision'] and not changed['tools']
    finally:
        await client.close();db.close()


async def test_text_fabricated_tool_call_and_provider_failure_are_diagnostic(tmp_path):
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config');db=Database(tmp_path/'test.db',recover=False)
    def respond(request):
        if request.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':'x','digest':'one'}]})
        if request.url.path=='/api/show': return httpx.Response(200,json={'capabilities':['tools','vision']})
        body=json.loads(request.content)
        if body.get('tools'): return httpx.Response(200,json={'message':{'content':'{"name":"echo_test","arguments":{"value":"blackbox-probe"}}'}})
        return httpx.Response(500,json={'error':'model requires more memory'})
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        result=await client.probe('x')
        assert not result['tools'] and not result['vision']
        assert result['diagnostics']['tools']['code']=='invalid_response'
        assert result['diagnostics']['vision']['code']=='memory_exhausted'
        assert 'memory' in result['diagnostics']['vision']['message']
    finally:
        await client.close();db.close()


async def test_verified_image_upload_reaches_model_and_renders(client,app,mock_ollama,png):
    from test_api import new,finish
    capability=await app.state.ollama.capabilities('fixture:4b')
    app.state.db.execute('UPDATE model_capabilities SET probe_json=? WHERE model_digest=?',
                         (json.dumps({'vision':True,'tools':True}),capability['digest']))
    app.state.ollama.cache=None
    sid=await new(client)
    result=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Describe image'},files={'images':('image.png',png,'image/png')})
    assert result.status_code==200,result.text
    state=await finish(app,sid)
    assert state['status']=='completed'
    assert mock_ollama.vision_requests
    images=mock_ollama.vision_requests[0]['messages'][0]['images']
    assert base64.b64decode(images[0]).startswith(b'\x89PNG\r\n\x1a\n')
    assert 'red square' in app.state.db.messages(sid)[-1]['content']


async def test_image_upload_rechecks_model_digest_even_with_a_warm_cache(client, app, mock_ollama, png):
    model = 'fixture:4b'
    assert (await app.state.ollama.capabilities(model))['vision']
    session = await client.post('/api/sessions', json={'model': model})
    sid = session.json()['id']

    def replaced(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': model, 'digest': 'replacement'}]})
        return mock_ollama(request)

    await app.state.ollama.http.aclose()
    app.state.ollama.http = httpx.AsyncClient(base_url='http://fixture', transport=httpx.MockTransport(replaced))
    result = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Describe image'},
                               files={'images': ('image.png', png, 'image/png')})
    assert result.status_code == 400
    assert 'Vision is advertised but unverified' in result.text
    assert not mock_ollama.vision_requests
    assert not app.state.db.rows('SELECT id FROM runs')
    assert not app.state.db.rows('SELECT id FROM attachments')


async def test_refresh_rechecks_authoritative_metadata(tmp_path):
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config');db=Database(tmp_path/'test.db',recover=False)
    advertised=['vision','tools']
    def respond(request):
        if request.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':'x','digest':'one'}]})
        return httpx.Response(200,json={'capabilities':advertised})
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        await client.models()
        db.execute('UPDATE model_capabilities SET probe_json=?',(json.dumps({'vision':True,'tools':True}),))
        advertised[:]=['tools']
        current=(await client.models(refresh=True))[0]
        assert not current['vision_advertised'] and not current['vision']
        assert current['tools_advertised'] and current['tools']
    finally:
        await client.close();db.close()


@pytest.mark.parametrize('kind,exception,expected',[
    ('timeout',httpx.ReadTimeout('slow'),'timeout'),
    ('connection',httpx.ConnectError('refused'),'connection_failed'),
])
async def test_probe_transport_errors_are_not_silent(tmp_path,kind,exception,expected):
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config');db=Database(tmp_path/'test.db',recover=False)
    def respond(request):
        if request.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':'x','digest':'one'}]})
        if request.url.path=='/api/show': return httpx.Response(200,json={'capabilities':['vision']})
        raise exception
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        outcome=await client.probe('x')
        assert not outcome['vision']
        assert outcome['diagnostics']['vision']['code']==expected
        assert outcome['diagnostics']['vision']['message']
    finally:
        await client.close();db.close()


async def test_model_replaced_during_probe_is_never_verified(tmp_path):
    from blackbox.core.ollama_client import OllamaError
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config');db=Database(tmp_path/'test.db',recover=False)
    tags_calls=0
    def respond(request):
        nonlocal tags_calls
        if request.url.path=='/api/tags':
            tags_calls+=1
            return httpx.Response(200,json={'models':[{'name':'x','digest':'first' if tags_calls==1 else 'replacement'}]})
        if request.url.path=='/api/show': return httpx.Response(200,json={'capabilities':['vision']})
        return httpx.Response(200,json={'message':{'content':'red square'}})
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        with pytest.raises(OllamaError,match='identity changed'):
            await client.probe('x')
        saved=db.one('SELECT probe_json FROM model_capabilities WHERE model_digest=?',('first',))
        assert json.loads(saved['probe_json'])=={}
    finally:
        await client.close();db.close()


async def test_model_loading_error_is_reported(tmp_path):
    settings=Settings(data_dir=tmp_path,config_dir=tmp_path/'config');db=Database(tmp_path/'test.db',recover=False)
    def respond(request):
        if request.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':'x','digest':'one'}]})
        if request.url.path=='/api/show': return httpx.Response(200,json={'capabilities':['vision']})
        return httpx.Response(503,json={'error':'model is still loading'})
    client=OllamaClient(settings,db,httpx.MockTransport(respond))
    try:
        result=await client.probe('x')
        assert result['diagnostics']['vision']['code']=='model_loading'
        assert 'still loading' in result['diagnostics']['vision']['message']
    finally:
        await client.close();db.close()


@pytest.mark.parametrize(('content', 'verified'), [
    ('**Red square.**', True),
    ('A solid red square', True),
    ('Predominantly blue square', False),
    ('There is no red square in the image.', False),
    ('Blue square, with a red background.', False),
    ('', False),
])
async def test_vision_probe_requires_affirmative_identification(tmp_path, content, verified):
    db = Database(tmp_path/'test.db', recover=False)

    def respond(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'vision:test', 'digest': 'one'}]})
        if request.url.path == '/api/show':
            return httpx.Response(200, json={'capabilities': ['vision']})
        return httpx.Response(200, json={'message': {'thinking': 'red square', 'content': content}})

    settings = Settings(data_dir=tmp_path, config_dir=tmp_path/'config')
    client = OllamaClient(settings, db, httpx.MockTransport(respond))
    try:
        result = await client.probe('vision:test')
        assert result['vision'] is verified
        assert ('vision' not in result['diagnostics'] if verified
                else result['diagnostics']['vision']['code'] == 'invalid_response')
    finally:
        await client.close()
        db.close()


@pytest.mark.parametrize('stage', ['second', 'final'])
async def test_probe_rejects_broken_multiturn_or_final_answer(tmp_path, stage):
    db=Database(tmp_path/'test.db',recover=False)
    def respond(request):
        if request.url.path=='/api/tags':
            return httpx.Response(200,json={'models':[{'name':'namespace/qwen:4b','digest':'one'}]})
        if request.url.path=='/api/show':
            return httpx.Response(200,json={'capabilities':['tools']})
        body=json.loads(request.content)
        if not body.get('tools'):
            return httpx.Response(200,json={'message':{'thinking':'private', 'content':''}})
        second=len(body['messages'])>1
        value='wrong' if second and stage=='second' else 'blackbox-second' if second else 'blackbox-probe'
        return httpx.Response(200,json={'message':{'tool_calls':[{'function':{'name':'echo_test','arguments':{'value':value}}}]}})
    client=OllamaClient(Settings(data_dir=tmp_path,config_dir=tmp_path/'config'),db,httpx.MockTransport(respond))
    try:
        result=await client.probe('namespace/qwen:4b')
        assert not result['tools']
        assert result['diagnostics']['tools']['code']==('multi_turn_failed' if stage=='second' else 'final_answer_failed')
    finally:
        await client.close()
        db.close()


async def test_saved_missing_model_restores_history_and_offers_exact_alternatives(client,app,mock_ollama):
    sid=(await client.post('/api/sessions',json={'model':'namespace/removed:4b'})).json()['id']
    restored=(await client.get(f'/api/sessions/{sid}')).json()
    assert restored['model']=='namespace/removed:4b'
    assert restored['model_availability']['status']=='missing'
    assert restored['model_availability']['alternatives']==['fixture:4b']
    result=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Hello'})
    assert result.status_code==503 and 'Installed alternatives: fixture:4b' in result.text
    assert not app.state.db.rows('SELECT * FROM runs')
    failed=await client.patch(f'/api/sessions/{sid}',json={'model':'another/missing:latest'})
    assert failed.status_code==503
    assert app.state.db.session(sid)['model']=='namespace/removed:4b'
    assert (await client.patch(f'/api/sessions/{sid}',json={'model':'fixture:4b'})).status_code==200
    assert (await client.get(f'/api/sessions/{sid}')).json()['model']=='fixture:4b'


async def test_restoration_during_provider_failure_does_not_lose_selected_model(client,app,mock_ollama):
    sid=(await client.post('/api/sessions',json={'model':'fixture:4b'})).json()['id']
    app.state.ollama.cache=None
    mock_ollama.unavailable=True
    restored=await client.get(f'/api/sessions/{sid}')
    assert restored.status_code==200
    assert restored.json()['model']=='fixture:4b'
    assert restored.json()['model_availability']['status']=='unknown'


async def test_namespace_and_tag_are_preserved_from_supported_tags_api(tmp_path):
    db=Database(tmp_path/'test.db',recover=False)
    names=['huihui_ai/qwen3.5-abliterated:4b','org/vision:latest']
    seen=[]
    def respond(request):
        seen.append(request.url.path)
        if request.url.path=='/api/tags':
            return httpx.Response(200,json={'models':[{'name':name,'digest':str(i)} for i,name in enumerate(names)]})
        model=json.loads(request.content)['model']
        return httpx.Response(200,json={'capabilities':['vision'] if model==names[1] else ['tools']})
    client=OllamaClient(Settings(data_dir=tmp_path,config_dir=tmp_path/'config'),db,httpx.MockTransport(respond))
    try:
        installed=await client.models()
        assert [m['name'] for m in installed]==names
        assert not installed[0]['vision_advertised'] and installed[1]['vision_advertised']
        assert seen==['/api/tags','/api/show','/api/show']
    finally:
        await client.close();db.close()


async def test_failed_vision_probe_blocks_upload_with_distinct_diagnostic(client,app,png):
    sid=(await client.post('/api/sessions',json={'model':'fixture:4b'})).json()['id']
    app.state.db.execute('UPDATE model_capabilities SET probe_json=?',
        (json.dumps({'vision':False,'tools':True,'diagnostics':{'vision':{'code':'timeout','message':'Controlled timeout'}}}),))
    app.state.ollama.cache=None
    response=await client.post(f'/api/sessions/{sid}/messages',data={'text':'Describe screenshot'},files={'images':('image.png',png,'image/png')})
    assert response.status_code==400
    assert 'Vision probe failed (timeout)' in response.text
    assert 'does not confirm lack of vision' in response.text
    assert not app.state.db.rows('SELECT * FROM runs')
