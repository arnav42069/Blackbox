from __future__ import annotations

import asyncio
import json
import re

from ..storage.db import now, uid
from ..storage.files import clean_text, clean_value
from ..tools.registry import Registry, ToolContext
from .scope import Scope
from .context import complete_tool_messages, bound_context
from .completion import CompletionReview, REVIEW_TOOL, REVIEW_INSTRUCTION, unfinished_promise


def parse_calls(message):
    raw = message.get('tool_calls', [])
    if not isinstance(raw, list) or len(raw)>20:
        raise ValueError('Malformed tool_calls list')
    calls = []
    for call in raw:
        f = call['function']
        name, args = f['name'], f.get('arguments', {})
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_.]{1,80}', name):
            raise ValueError('Malformed tool name')
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(args, dict) or len(json.dumps(args))>40000:
            raise ValueError('Malformed tool arguments')
        calls.append({'function': {'name': name, 'arguments': args}})
    return calls


def visible_content(value):
    if not isinstance(value, str):
        raise ValueError('Malformed model content')
    return clean_text(re.sub(r'<think>.*?(?:</think>|$)', '', value, flags=re.S)).strip()[:32000]


class Agent:
    def __init__(self, settings, db, ollama):
        self.settings, self.db, self.ollama = settings, db, ollama
        self.tasks: dict[str, asyncio.Task] = {}
        self.registry = Registry()
        self.approvals = {}
        self.plans = {}

    def active(self, sid):
        task = self.tasks.get(sid)
        return bool(task and not task.done())

    def start(self, sid, rid):
        self.tasks[sid] = asyncio.create_task(self.run(sid, rid))

    async def cancel(self, sid):
        task = self.tasks.get(sid)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its first instruction cannot run its own finally block.
            for run in self.db.rows("SELECT id FROM runs WHERE session_id=? AND status='running'", (sid,)):
                self.db.execute("UPDATE runs SET status='cancelled',ended_at=? WHERE id=?", (now(), run['id']))
                self.db.execute("UPDATE sessions SET status='cancelled' WHERE id=?", (sid,))
                self.event(sid, run['id'], 'run_cancelled', reason='Stopped before run initialization')

    async def shutdown(self):
        for sid in list(self.tasks):
            await self.cancel(sid)

    def event(self, sid, rid, kind, **payload):
        return self.db.event(sid, rid, kind, payload)

    async def history(self, sid):
        messages = [{'role': 'system', 'content': self.settings.prompt_path.read_text()}]
        # Load from user boundaries; never start with a dangling assistant/tool message.
        rows = self.db.messages(sid, 300)
        groups = []
        for row in rows:
            if row['role'] == 'user':
                groups.append([])
            if not groups:
                continue
            message = {'role': row['role'], 'content': clean_text(row['content']), **row['metadata']}
            message.pop('thinking', None)
            message['_attachment_ids'] = row['attachment_ids']
            groups[-1].append(message)
        size, chosen = 0, []
        budget = min(24000, self.settings.prefs.num_ctx * 3)
        for group in reversed(groups):
            cost = sum(len(m.get('content', '')) + len(json.dumps(m.get('tool_calls', []))) + (1500 * len(m.get('_attachment_ids', []))) for m in group)
            if chosen and size + cost > budget:
                break
            chosen.insert(0, group)
            size += cost
        if len(chosen) < len(groups):
            summary = 'Earlier user goals (extractive, untrusted): ' + '\n'.join(g[0].get('content', '')[:200] for g in groups[:-len(chosen)])[-1800:]
            messages.append({'role': 'user', 'content': summary})
        for group in chosen:
            messages.extend(complete_tool_messages(group))
        # Only current-turn images are needed for tool continuation. Older images stay in UI history.
        latest = max((i for i,m in enumerate(messages) if m['role']=='user'), default=0)
        import base64
        from pathlib import Path
        for i, message in enumerate(messages):
            attachment_ids = message.pop('_attachment_ids', [])
            if i == latest and attachment_ids:
                image_rows = [self.db.one('SELECT path FROM attachments WHERE id=? AND session_id=?', (aid, sid)) for aid in attachment_ids]
                message['images'] = [base64.b64encode(Path(row['path']).read_bytes()).decode() for row in image_rows if row]
        return messages

    async def run(self, sid, rid):
        status, metrics, model_turns = 'failed', {}, 0
        ctx = ToolContext(self.settings, self.db, sid, rid, Scope(self.db.session(sid)['scope']), self.registry.runner, self)
        try:
            self.event(sid, rid, 'run_started')
            self.event(sid, rid, 'assistant_status', value='checking scope')
            self.plans[sid] = {'goal': self.db.messages(sid, 1)[0]['content'][:200], 'next_step': 'Ask model for bounded next action', 'last_evidence': None, 'completion_condition': 'Evidence-backed answer or explicit blocking reason'}
            async with asyncio.timeout(self.settings.prefs.max_wall_seconds):
                session = self.db.session(sid)
                cap = await self.ollama.capabilities(session['model'])
                history = await self.history(sid)
                if any(m.get('images') for m in history) and not cap['vision']:
                    raise ValueError('This model has not verified vision support. Choose/probe a vision model before sending images.')
                from ..tools.ssh import public_profiles
                try:
                    access = public_profiles(self.settings)
                except ValueError:
                    access = []
                history[0]['content'] += '\nUser-approved scope and access metadata (no credentials): ' + json.dumps({'scope': session['scope'], 'ssh_profiles': access})
                failures, repair, observation_size = {}, False, 0
                verified_reads, verified_hosts = [], []
                pending_answer, review_repair = None, False
                for turn in range(self.settings.prefs.max_turns):
                    reviewing = pending_answer is not None
                    model_turns += 1
                    self.event(sid, rid, 'assistant_status', value='reviewing completion' if reviewing else 'thinking')
                    schemas = self.registry.schemas() if cap['tools'] else None
                    request_history = history + [{'role': 'system', 'content': REVIEW_INSTRUCTION}] if reviewing else history
                    response = await self.ollama.chat(session['model'], bound_context(request_history, self.settings.prefs.num_ctx), [REVIEW_TOOL] if reviewing else schemas)
                    metrics = {k: response[k] for k in ('total_duration', 'load_duration', 'prompt_eval_count', 'eval_count') if k in response}
                    if reviewing:
                        try:
                            control = parse_calls(response['message'])
                            if len(control) != 1 or control[0]['function']['name'] != 'agent.review_completion':
                                raise ValueError('Expected one structured completion review')
                            review = CompletionReview.model_validate(control[0]['function']['arguments'])
                        except (ValueError, KeyError, TypeError) as e:
                            if review_repair:
                                raise ValueError('Cannot verify task completion: model returned an invalid completion review twice') from e
                            review_repair = True
                            self.event(sid, rid, 'completion_review', decision='invalid', reason='Retrying malformed completion control response')
                            history.append({'role': 'system', 'content': 'Return exactly one agent.review_completion call with decision, reason and remaining_steps. This is a control assessment, not an executable action.'})
                            continue
                        veto = unfinished_promise(pending_answer)
                        decision = 'continue' if veto else review.decision
                        self.event(sid, rid, 'completion_review', decision=decision, model_decision=review.decision, reason='Draft promises unexecuted work' if veto else clean_text(review.reason), remaining_steps=clean_value(review.remaining_steps))
                        if decision == 'complete':
                            self.finish_answer(sid, rid, self.with_evidence(pending_answer, verified_reads, verified_hosts))
                            status = 'completed'
                            break
                        history.append({'role': 'system', 'content': 'Controller: task is not finished. ' + ('Your draft promised further execution. ' if veto else '') + clean_text(review.reason) + '\nRemaining steps (not commands to execute): ' + json.dumps(clean_value(review.remaining_steps)) + '\nUse registered tools for necessary remaining work, or replace the draft with an evidence-grounded answer if all requested checks already ran. Do not repeat completed checks or add unrelated work. Never claim completion while promising future commands. Only the executor can run actions; scope and budgets still apply.'})
                        pending_answer = None
                        continue
                    try:
                        message = response['message']
                        content = visible_content(message.get('content', ''))
                        calls = clean_value(parse_calls(message))
                    except (KeyError, TypeError, ValueError) as e:
                        if repair:
                            raise ValueError('Model returned malformed tool calls twice; try a compatible model') from e
                        repair = True
                        history.append({'role': 'system', 'content': 'Your tool-call response was malformed. Retry once with registered tools and valid JSON object arguments.'})
                        continue
                    metrics = {k: response[k] for k in ('total_duration', 'load_duration', 'prompt_eval_count', 'eval_count') if k in response}
                    if calls and not cap['tools']:
                        raise ValueError('Tool capability is unverified. Run the model probe before using tools.')
                    if not calls:
                        if not content:
                            raise ValueError('Model returned an empty answer; try another model or disable thinking')
                        if cap['tools']:
                            # A text-only model turn is a draft. It can be a plan, not a final answer.
                            pending_answer = content
                            history.append({'role': 'assistant', 'content': content})
                            self.event(sid, rid, 'assistant_progress', text=content)
                            continue
                        if unfinished_promise(content):
                            raise ValueError('Model promised tool execution but has no verified tool capability; select/probe a tool-capable model')
                        self.finish_answer(sid, rid, content)
                        status = 'completed'
                        break
                    metadata = {'tool_calls': calls}
                    self.db.message(sid, 'assistant', content, metadata=metadata)
                    history.append({'role': 'assistant', 'content': content, **metadata})
                    if content:
                        self.event(sid, rid, 'assistant_progress', text=content)
                    for call in calls:
                        if ctx.calls >= self.settings.prefs.max_tool_calls:
                            raise ValueError('Tool call budget exhausted; partial evidence is retained')
                        name, args = call['function']['name'], call['function']['arguments']
                        self.plans[sid]['next_step'] = name
                        result, tid = await self.registry.execute(ctx, name, args)
                        observation = result.model_dump()
                        observation['tool_id'], observation['timestamp'] = tid, now()
                        observation['untrusted_observation'] = True
                        if name == 'ssh.read_file' and result.ok:
                            verified_reads.append({'tool_id': tid, 'timestamp': observation['timestamp'], 'target': args['target'], 'path': result.evidence[0]['path'], 'snippet': result.stdout[:300]})
                        is_host = name in {'host.preflight', 'host.diagnostics'}
                        if is_host:
                            if result.ok and result.evidence:
                                verified_hosts.append({'tool_id': tid, 'timestamp': observation['timestamp'], 'facts': result.evidence[0]})
                            # Keep factual fields intact, not an arbitrary excerpt of lscpu/raw output.
                            observation['stdout'] = 'Raw command output is in the audit log. Measured fields and statuses are below.'
                            observation['evidence'] = result.evidence[:1] + [{'checks': [{k: r[k] for k in ('check', 'ok', 'exit_code', 'error') if k in r} for r in result.evidence[1:]]}]
                        else:
                            observation['stdout'] = observation['stdout'][:6000]
                        observation['stderr'] = observation['stderr'][:1000]
                        if not is_host and len(json.dumps(observation['evidence'])) > 3000:
                            observation['evidence'] = [{'excerpt': json.dumps(observation['evidence'])[:3000]}]
                        text = json.dumps(observation)
                        observation_size += len(text)
                        tool_message = {'role': 'tool', 'tool_name': name, 'content': text}
                        self.db.message(sid, 'tool', text, metadata={'tool_name': name})
                        history.append(tool_message)
                        self.plans[sid]['last_evidence'] = tid
                        key = json.dumps(call, sort_keys=True)
                        failures[key] = 0 if result.ok else failures.get(key, 0)+1
                        if result.denied_reason:
                            status = 'denied'
                            self.finish_answer(sid, rid, f'Action denied: {result.denied_reason}. Evidence: {tid}.')
                            break
                        if 'needs access' in result.summary.lower():
                            status = 'needs_access'
                            self.finish_answer(sid, rid, f'{result.summary}. Evidence: {tid}.')
                            break
                        if failures[key] >= 2:
                            raise ValueError(f'{name} failed twice without new evidence; stopping')
                        if observation_size > 40000:
                            raise ValueError('Tool observation context budget reached; partial evidence is retained')
                    if status in {'denied', 'needs_access'}:
                        break
                else:
                    raise ValueError('Agent turn budget exhausted; partial evidence is retained')
            self.event(sid, rid, 'metrics', **metrics, tool_calls=ctx.calls, http_requests=ctx.requests, model_turns=model_turns)
            self.event(sid, rid, 'run_finished', reason=status)
        except asyncio.CancelledError:
            status = 'cancelled'
            self.event(sid, rid, 'run_cancelled', reason='Stopped by user; partial evidence retained')
        except Exception as e:
            message = 'Run wall-time limit reached' if isinstance(e, TimeoutError) else clean_text(str(e))[:1000]
            self.event(sid, rid, 'run_failed', message=message)
        finally:
            self.db.execute('UPDATE runs SET status=?, ended_at=?,counts_json=?,metrics_json=? WHERE id=?', (status, now(), json.dumps({'tool_calls': ctx.calls, 'http_requests': ctx.requests, 'model_turns': model_turns}), json.dumps(metrics), rid))
            self.db.execute('UPDATE sessions SET status=? WHERE id=?', (status, sid))
            self.plans.pop(sid, None)

    @staticmethod
    def with_evidence(content, reads, hosts):
        if reads:
            content += '\n\nVerified SSH read evidence:\n```json\n' + json.dumps(reads[-6:], indent=2) + '\n```'
        if hosts:
            content += '\n\nVerified host observations (measured by BlackBox):\n```json\n' + json.dumps(hosts[-1], indent=2) + '\n```'
        return content

    def finish_answer(self, sid, rid, text):
        self.db.message(sid, 'assistant', text)
        self.event(sid, rid, 'answer_delta', text=text)
