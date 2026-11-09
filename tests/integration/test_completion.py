import json

from conftest import call
from test_api import new, finish


def review(decision, reason='The requested follow-up checks remain.', remaining=None):
    return {'decision': decision, 'reason': reason, 'remaining_steps': remaining or []}


async def test_promised_diagnostics_continue_to_real_execution(client, app, mock_ollama):
    sid = await new(client)
    mock_ollama.replies = [call('host.preflight'), {'content': "I'll run uname and free next and report the exact output."},
                          call('host.diagnostics', sections=['system','memory']), {'content': 'Requested kernel and RAM checks have now executed; see measured results.'}]
    # Even a model rubber-stamping its own promise must not end the run.
    mock_ollama.review_replies = [review('complete'), review('complete', 'All requested checks executed.')]
    await client.post(f'/api/sessions/{sid}/messages', data={'text':'Check actual kernel and memory and verify both with host diagnostics.'})
    assert (await finish(app,sid))['status']=='completed'
    records = app.state.db.rows('SELECT * FROM tool_events ORDER BY started_at')
    assert [r['tool_name'] for r in records] == ['host.preflight','host.diagnostics']
    assert all(r['status']=='success' for r in records)
    events = app.state.db.events(sid)
    decisions = [e['decision'] for e in events if e['type']=='completion_review']
    assert decisions == ['continue','complete']
    commands = [e['argv'] for e in events if e['type']=='tool_command']
    assert any(argv[0].endswith('/uname') for argv in commands)
    assert any(argv[0].endswith('/free') for argv in commands)
    final = app.state.db.messages(sid)[-1]['content']
    assert "I'll run" not in final and 'Verified host observations' in final
    assert not any('agent.review_completion' in r['tool_name'] for r in records)


async def test_review_detects_missing_step_even_without_future_tense(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('host.preflight'),{'content':'Preflight is done.'},call('workspace.list'),{'content':'Preflight and workspace listing completed.'}]
    mock_ollama.review_replies=[review('continue',remaining=['List the requested workspace']),review('complete','All requested steps completed.')]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Run preflight and list the workspace.'})
    assert (await finish(app,sid))['status']=='completed'
    assert [r['tool_name'] for r in app.state.db.rows('SELECT tool_name FROM tool_events ORDER BY started_at')]==['host.preflight','workspace.list']


async def test_continuation_cannot_expand_scope(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('host.preflight'),{'content':"I'll inspect the target next."},call('web.inspect',url='http://8.8.8.8/')]
    mock_ollama.review_replies=[review('continue',remaining=['Inspect target'])]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Check host and inspect target.'})
    assert (await finish(app,sid))['status']=='denied'
    assert any(e['type']=='tool_denied' for e in app.state.db.events(sid))


async def test_continuation_obeys_turn_and_tool_budgets(client,app,mock_ollama):
    sid=await new(client)
    app.state.settings.prefs.max_turns=3
    mock_ollama.replies=[call('host.preflight'),{'content':"I'll run diagnostics next."},call('host.diagnostics',sections=['gpu'])]
    mock_ollama.review_replies=[review('continue',remaining=['GPU diagnostics'])]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Check host and GPU.'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==1
    assert len(mock_ollama.requests)+len(mock_ollama.review_requests)==3
    assert any('turn budget exhausted' in e.get('message','') for e in app.state.db.events(sid))
    app.state.settings.prefs.max_turns=12
    app.state.settings.prefs.max_tool_calls=1
    mock_ollama.replies=[call('host.preflight'),{'content':"I'll run diagnostics next."},call('host.diagnostics',sections=['gpu'])]
    mock_ollama.review_replies=[review('continue',remaining=['GPU diagnostics'])]
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Try within the same limits.'})
    assert (await finish(app,sid))['status']=='failed'
    assert len(app.state.db.rows('SELECT * FROM tool_events'))==2


async def test_bad_completion_review_is_not_silent_success(client,app,mock_ollama):
    sid=await new(client)
    mock_ollama.replies=[call('host.preflight'),{'content':'Done.'}]
    mock_ollama.review_replies=[{'content':'Done, trust me.'}]*2
    await client.post(f'/api/sessions/{sid}/messages',data={'text':'Check host.'})
    assert (await finish(app,sid))['status']=='failed'
    assert any('Cannot verify task completion' in e.get('message','') for e in app.state.db.events(sid))
