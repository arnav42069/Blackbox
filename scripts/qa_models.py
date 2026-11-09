"""Real localhost inference: capability metadata alone is never a passing probe."""
import asyncio
import base64
import io
import json
import sys
import tempfile
from pathlib import Path
import httpx
from PIL import Image, ImageDraw
from blackbox.app import create_app
from blackbox.settings import Settings
from scripts.qa_support import write_json, agent_findings


async def main(model, destination):
    evidence = {'model': model, 'source': 'real_local_ollama', 'checks': {}, 'errors': []}
    with tempfile.TemporaryDirectory(prefix='blackbox-qa-model-') as directory:
        root = Path(directory)
        settings = Settings(data_dir=root / 'data', config_dir=root / 'config', ollama_url='http://127.0.0.1:11434')
        settings.prefs.model = model
        app = create_app(settings)
        try:
            cap = await app.state.ollama.capabilities(model)
            evidence['digest'] = cap['digest']
            # Probe a non-red randomized color to avoid accepting a memorized red-square answer.
            import secrets
            color = secrets.choice(['blue', 'green', 'yellow'])
            image = Image.new('RGB', (96, 96), 'white')
            ImageDraw.Draw(image).rectangle((16, 16, 80, 80), fill=color)
            output = io.BytesIO()
            image.save(output, format='PNG')
            reply = await app.state.ollama.chat(model, [{'role': 'user', 'content': 'Name the color and shape inside the image in two English words.', 'images': [base64.b64encode(output.getvalue()).decode()]}])
            content = reply.get('message', {}).get('content', '').lower()
            evidence['checks']['vision'] = {'passed': color in content and any(shape in content for shape in ('square', 'rectangle')), 'expected_color': color, 'response': content[:300]}
            probes = await app.state.ollama.probe(model)
            evidence['application_probes'] = probes
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765', headers={'Origin': 'http://127.0.0.1:8765'}) as client:
                pair = await client.post('/api/pair', json={'token': app.state.auth.pairing})
                pair.raise_for_status()
                client.headers['Authorization'] = 'Bearer ' + pair.json()['token']
                session = await client.post('/api/sessions', json={'model': model})
                sid = session.json()['id']
                response = await client.post(f'/api/sessions/{sid}/messages', data={'text': 'Call host.preflight once and summarize its measured kernel. This is the entire task; stop after the summary. No scans, network tools, file inspection, or next steps are requested. Use actual evidence.'})
                response.raise_for_status()
                await app.state.agent.tasks[sid]
                tools = app.state.db.rows('SELECT * FROM tool_events')
                events = app.state.db.events(sid)
                terminal = app.state.db.session(sid)['status']
                findings = agent_findings(events, tools, required_tools=['host.preflight'], terminal=terminal)
                import os
                output = json.loads(next((t['output_json'] for t in tools if t['tool_name'] == 'host.preflight'), '{}'))
                measured = output.get('evidence', [{}])[0].get('kernel')
                if measured != os.uname().release:
                    findings.append({'category': 'fabricated_tool_output', 'detail': 'Executor kernel does not match os.uname()'})
                evidence['checks']['tools'] = {'passed': not findings and probes.get('tools') is True, 'terminal': terminal, 'findings': findings}
                evidence['checks']['agent_completion'] = {'passed': terminal == 'completed', 'terminal': terminal, 'detail': 'Only host.preflight was requested. Extra scans are not authorized by this QA task.'}
                evidence.update(tools=tools, events=events)
        except Exception as exc:
            evidence['errors'].append(f'{type(exc).__name__}: {exc}')
        finally:
            await app.state.agent.shutdown()
            await app.state.ollama.close()
            app.state.db.close()
            write_json(destination, evidence)
    return 0 if not evidence['errors'] and len(evidence['checks']) == 3 and all(c['passed'] for c in evidence['checks'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2])))
