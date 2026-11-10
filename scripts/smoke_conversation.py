#!/usr/bin/env python3
"""Read-only live-model smoke through an isolated BlackBox app, never the user's run.

No scope is granted. Saved capability probes are copied by exact model digest;
private user messages, credentials and scopes are not read or copied.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from blackbox.app import create_app
from blackbox.settings import Settings
from blackbox.storage.db import now,uid


async def main(model,output):
    actual=Settings().data_dir/'blackbox.db'
    with tempfile.TemporaryDirectory(prefix='blackbox-conversation-smoke-') as root:
        settings=Settings(data_dir=Path(root)/'data',config_dir=Path(root)/'config')
        settings.prefs.model=model
        settings.prefs.max_wall_seconds=45
        settings.prefs.max_turns=4
        settings.prefs.max_tool_calls=2
        app=create_app(settings)
        try:
            cap=await app.state.ollama.capabilities(model)
            if actual.exists():
                conn=sqlite3.connect(f'file:{actual}?mode=ro',uri=True)
                try:
                    row=conn.execute('SELECT probe_json FROM model_capabilities WHERE model_digest=?',(cap['digest'],)).fetchone()
                finally:
                    conn.close()
                if row:
                    app.state.db.execute('UPDATE model_capabilities SET probe_json=? WHERE model_digest=?',(row[0],cap['digest']))
                    app.state.ollama.cache=None
            cap=await app.state.ollama.capabilities(model)
            db=app.state.db
            sid=db.create_session(model)['id']
            rid=uid()
            db.message(sid,'user','Hello. Reply only with: BlackBox conversational response is working.')
            db.execute("INSERT INTO runs VALUES (?,?,?,NULL,'running','{}','{}')",(rid,sid,now()))
            db.execute("UPDATE sessions SET status='running' WHERE id=?",(sid,))
            started=time.monotonic()
            app.state.agent.start(sid,rid)
            await app.state.agent.tasks[sid]
            messages=db.messages(sid)
            events=db.events(sid)
            finals=[m for m in messages if m['metadata'].get('conversation_kind')=='final']
            result={'model':model,'capabilities':{k:cap[k] for k in ('tools','vision','source')},
                'seconds':round(time.monotonic()-started,2),'status':db.session(sid)['status'],
                'streamed_deltas':sum(e['type']=='answer_delta' for e in events),
                'model_turns':json.loads(db.one('SELECT counts_json FROM runs WHERE id=?',(rid,))['counts_json'])['model_turns'],
                'final':finals[-1]['content'] if finals else None,
                'terminal_events':[e['type'] for e in events if e['type'] in {'run_failed','run_finished','run_cancelled'}],
                'tool_count':len(db.rows('SELECT * FROM tool_events'))}
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(result,indent=2))
            print(json.dumps(result,indent=2),flush=True)
            return 0 if result['status']=='completed' and result['final'] and result['streamed_deltas'] else 1
        finally:
            await app.state.agent.shutdown()
            await app.state.ollama.close()
            app.state.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--report',type=Path,default=Path('qa-reports/conversation-live-ollama.json'))
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.model,args.report)))
