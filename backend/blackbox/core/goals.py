"""Persistent, deterministic goal controller over the existing policy-enforced tools.

Discovery is not exploitation: only registered scans, public HTTP and authorized
read-only SFTP are available. Tool outputs never create permissions or commands.
"""
from __future__ import annotations

import json
import re
import ipaddress
import time
from urllib.parse import urlsplit, urljoin
from html.parser import HTMLParser

from pydantic import BaseModel, Field

from .planner import Planner, explicit_target, http_candidates
from .scans import ScanStore, scope_key, normalized_args
from .scope import ports, ScopeDenied, port_expression
from ..storage.db import now, uid
from ..storage.files import clean_value


def goal_request(text):
    room = bool(re.search(r'\b(solve|complete)\b.*\b(room|lab|tryhackme)\b', text, re.I) or
                (re.search(r'\btryhackme\b', text, re.I) and re.search(r'\b(find|retrieve|recover|solve|complete)\b', text, re.I)))
    if room:
        return True
    if re.search(r'\b(ssh|sftp|profile|screenshot|image|diagram)\b',text,re.I):
        return False
    return bool(re.search(r'\b(find|retrieve|recover|fetch|read)\b.*\bflag(?:\.txt)?\b',text,re.I))


def continuation(text):
    return bool(re.fullmatch(r'\s*(?:continue|resume)(?:\s+(?:task|goal|progress))?[.!]?\s*', text, re.I))


def evidence_ports(value):
    # Normalized cache/action keys use expanded comma lists, which can exceed
    # the live command schema's 200-character bound. Never execute that string.
    if isinstance(value,str) and len(value)<=6000 and re.fullmatch(r'\d+(?:,\d+)*',value):
        values={int(part) for part in value.split(',')}
        if len(values)<=1000 and all(1<=port<=65535 for port in values):
            return values
    return ports(value)


class Action(BaseModel):
    tool: str
    arguments: dict
    rationale: str

    def key(self):
        return json.dumps([self.tool, normalized_args(self.tool,self.arguments)],sort_keys=True)


class TaskGoal(BaseModel):
    id: str = Field(default_factory=uid)
    instruction: str
    target: str | None = None
    filename: str = 'flag.txt'
    scope_key: str = ''
    status: str = 'active'
    decomposition: list[str] = Field(default_factory=lambda:[
        'Retrieve relevant scoped evidence', 'Identify permitted services',
        'Inspect public HTTP and bounded content discovery',
        'Use configured read-only SFTP only if needed', 'Verify candidate file with tool evidence'])
    attempts: dict[str, dict] = Field(default_factory=dict)
    hypotheses: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    remote_files: list[dict] = Field(default_factory=list)
    pending: Action | None = None
    result: dict | None = None
    checkpoint: dict = Field(default_factory=dict)
    phase: str = 'PLAN'
    objective_kind: str = 'artifact'
    completion_criteria: list[str] = Field(default_factory=lambda:['Read actual artifact contents with attributable tool evidence','Do not claim challenge acceptance without independent verification'])
    verified_findings: list[dict] = Field(default_factory=list)
    pending_steps: list[Action] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
    last_run_id: str | None = None


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links=[]

    def handle_starttag(self,tag,attrs):
        if tag=='a' and len(self.links)<32:
            self.links.extend(v for k,v in attrs if k=='href' and v)


class GoalController:
    def __init__(self,agent,ctx):
        self.agent,self.ctx=agent,ctx
        self.state=None
        self.started=time.monotonic()

    @staticmethod
    def load(db,sid):
        row=db.one('SELECT payload_json FROM task_goals WHERE session_id=?',(sid,))
        return TaskGoal.model_validate_json(row['payload_json']) if row else None

    def save(self):
        self.state.last_run_id=self.ctx.rid
        self.state.budget={'tool_calls_remaining':max(0,self.ctx.settings.prefs.max_tool_calls-self.ctx.calls),
                          'http_requests_remaining':max(0,self.ctx.scope.data.get('max_requests_per_run',0)-self.ctx.requests),
                          'wall_seconds_remaining':max(0,int(self.ctx.settings.prefs.max_wall_seconds-(time.monotonic()-self.started)))}
        self.ctx.db.execute('INSERT OR REPLACE INTO task_goals VALUES (?,?,?)',
            (self.ctx.sid,json.dumps(clean_value(self.state.model_dump())),now()))

    def transition(self,phase):
        self.state.phase=phase
        self.save()
        self.agent.event(self.ctx.sid,self.ctx.rid,'goal_state',goal_id=self.state.id,target=self.state.target,phase=phase,budget=self.state.budget,at=now())

    def limit(self,text):
        if text not in self.state.limitations:
            self.state.limitations.append(text[:500])

    def checkpoint(self,reason,status='checkpointed'):
        self.state.status=status
        self.state.checkpoint={'reason':reason,'at':now(),'run_id':self.ctx.rid,
            'tool_calls':self.ctx.calls,'http_requests':self.ctx.requests,
            'pending':self.state.pending.model_dump() if self.state.pending else None}
        self.save()
        self.agent.event(self.ctx.sid,self.ctx.rid,'goal_checkpoint',goal=self.state.model_dump(),reason=reason)
        pending=self.state.pending
        self.agent.finish_answer(self.ctx.sid,self.ctx.rid,
            f'Goal checkpoint saved: {reason}. No unverified flag or access is claimed. '
            'Send “Continue task” for one new bounded run; scope and all per-run limits will be checked again.\n\n'
            f'**Goal:** {self.state.instruction}\n\n'
            f'**Execution history:** {len(self.state.attempts)} recorded actions; {sum(bool(a.get("advances_investigation")) for a in self.state.attempts.values())} actions yielded relevant observations. The objective is not verified complete.\n\n'
            + '**Completion criteria:** '+'; '.join(self.state.completion_criteria)+'\n\n'
            + ('**Next action:** '+pending.tool+' — '+pending.rationale+'\n\n' if pending else '')
            + ('**Recorded limitations:**\n'+'\n'.join('- '+item for item in self.state.limitations) if self.state.limitations else ''))
        return status

    def complete(self):
        state=self.state
        state.status='completed'
        state.phase='EVALUATE'
        state.pending=None
        state.pending_steps=[]
        self.save()
        result=state.result
        self.agent.finish_answer(self.ctx.sid,self.ctx.rid,
            'Retrieved file candidate with verified tool provenance. Room completion is not independently verified.\n\n'
            + '**Candidate contents:**\n\n> '+result['content'].replace('\n','\n> ')+'\n\n'
            + f"**Source:** `{result.get('url',result.get('path',''))}`\n\n"
            + f"**Evidence:** `{result['tool_id']}`; observed at {result['at']}.\n\n"
            + f'**Actions performed:** {len(state.attempts)} recorded inspections/discovery steps.\n\n'
            + '**Outstanding:** verify candidate acceptance through the authorized room’s normal submission process.')
        return 'completed'

    def unused(self,action):
        spec=self.agent.registry.specs[action.tool]
        action.arguments=spec.argument_schema.model_validate(action.arguments).model_dump()
        prior=self.state.attempts.get(action.key())
        fresh=prior and (prior.get('run_id')==self.ctx.rid or (prior['ok'] and now()-prior['at']<=self.ctx.settings.prefs.scan_freshness_seconds))
        return action if not fresh else None

    async def next_action(self):
        ctx,s=self.ctx,self.state
        # Existing planner owns HTTP endpoint resolution, ambiguity and freshness.
        _, explicit_url=explicit_target(s.instruction)
        web=await Planner().build(ctx,'Fuzz '+explicit_url if explicit_url else 'Fuzz the web server on '+s.target)
        if not explicit_url:
            origins=http_candidates(await ScanStore(ctx.db).relevant(ctx,s.target))
            for origin in origins:
                urls=[origin,urljoin(origin,s.filename),*[url for url in s.candidates if urlsplit(url).netloc==urlsplit(origin).netloc]]
                pending=any(self.unused(Action(tool='web.inspect',arguments={'url':url},rationale='Test public candidate')) for url in urls)
                if not pending and ctx.scope.data.get('allow_active_scanning'):
                    discovery=await Planner().build(ctx,'Fuzz '+origin)
                    pending=discovery.state=='ready' and self.unused(Action(tool=discovery.tool,arguments=discovery.arguments,rationale=discovery.rationale))
                if pending:
                    web=await Planner().build(ctx,'Fuzz '+origin)
                    break
        if web.state=='ready':
            s.limitations=[message for message in s.limitations if not message.startswith(('No fresh, usable HTTP endpoint','Multiple HTTP endpoints'))]
            origin=web.arguments['url']
            for url in [origin, urljoin(origin,s.filename),*[url for url in s.candidates if urlsplit(url).netloc==urlsplit(origin).netloc]]:
                action=self.unused(Action(tool='web.inspect',arguments={'url':url},
                    rationale='Test a public file/service candidate; availability and flag content remain hypotheses until observed.'))
                if action:
                    return action
            if ctx.scope.data.get('allow_active_scanning'):
                action=self.unused(Action(tool=web.tool,arguments=web.arguments,rationale=web.rationale))
                if action:
                    return action
            else:
                self.limit('Active HTTP discovery is not permitted by the current scope.')
        else:
            self.limit(web.rationale)
        # Do not repeat a failed scan or a complete scan which found no HTTP.
        scan=await Planner().build(ctx,'Discover open ports on '+s.target)
        if scan.state=='ready' and ctx.scope.data.get('allow_active_scanning'):
            # Prioritize common web ports without exceeding the existing 1000-port cap.
            allowed=ports(ctx.scope.data['allowed_ports'])
            if len(allowed)>1000:
                self.limit('Each TCP discovery call is limited to 1000 authorized ports. Additional batches require remaining execution budget.')
            scan.arguments.update(service_detection=True)
            # Fresh existing scan evidence should prevent a prerequisite rescan.
            evidence=await ScanStore(ctx.db).relevant(ctx,s.target)
            covered=set()
            for key,attempt in s.attempts.items():
                name,args=json.loads(key)
                if name=='net.scan_ports' and (attempt.get('run_id')==ctx.rid or (attempt['ok'] and now()-attempt['at']<=ctx.settings.prefs.scan_freshness_seconds)):
                    covered.update(evidence_ports(args['ports']))
            for row in evidence:
                if row['tool']=='net.scan_ports' and row['source']=='execution' and row['status']=='success' and row['complete'] and row['freshness']=='fresh':
                    try:
                        covered.update(evidence_ports(row['arguments'].get('ports','')))
                    except ValueError:
                        pass
            missing=allowed-covered
            complete=not missing
            if missing:
                common=missing & {80,443,8080,8443}
                chosen=sorted(common or missing)[:1000]
                # Compact contiguous ranges to satisfy the existing argument bound.
                while len(port_expression(chosen))>200:
                    chosen.pop()
                scan.arguments['ports']=port_expression(chosen)
            action=self.unused(Action(tool=scan.tool,arguments=scan.arguments,rationale='Identify missing service prerequisites before deciding whether SSH is necessary.'))
            if action and not complete:
                return action
        else:
            self.limit('Port discovery is disabled or unavailable under current policy/tool availability.')
        if web.state!='ready':
            # An open TCP port is evidence of a listener, not of its application
            # protocol. A bounded HEAD can test HTTP without assuming SSH/access.
            for record in await ScanStore(ctx.db).relevant(ctx,s.target):
                if record['source']!='execution' or record['status']!='success' or not record['complete'] or record['freshness']!='fresh':
                    continue
                for finding in record['findings']:
                    if finding.get('kind')!='port' or finding.get('state')!='open' or finding.get('protocol')!='tcp' or finding.get('service') not in {'','unknown'}:
                        continue
                    host=f'[{s.target}]' if ':' in s.target else s.target
                    scheme='https' if finding['port'] in {443,8443} else 'http'
                    action=self.unused(Action(tool='web.inspect',arguments={'url':f'{scheme}://{host}:{finding['port']}/','method':'HEAD'},
                        rationale='Protocol hypothesis only: test an observed open unknown TCP service with bounded HTTP HEAD. No HTTP service or SSH access is assumed.'))
                    if action:
                        return action
        from ..tools.ssh import public_profiles
        for origin in http_candidates(await ScanStore(ctx.db).relevant(ctx,s.target)):
            if ctx.runner.binaries.get('whatweb'):
                action=self.unused(Action(tool='web.technologies',arguments={'url':origin},rationale='No artifact found yet; fingerprint the observed HTTP service for additional bounded evidence. Technology names alone are not confirmed vulnerabilities.'))
                if action:
                    return action
            if urlsplit(origin).scheme=='https' and ctx.runner.binaries.get('openssl'):
                action=self.unused(Action(tool='web.tls',arguments={'url':origin},rationale='Validate the observed HTTPS handshake within scope; a certificate issue is not proof of artifact access.'))
                if action:
                    return action
        try:
            profiles=[p for p in public_profiles(ctx.settings) if p['host']==s.target]
        except ValueError as e:
            profiles=[]
            self.limit(str(e))
        for file in s.remote_files:
            action=self.unused(Action(tool='ssh.read_file',arguments=file,rationale='Read a matching file located by authorized SFTP, without shell access.'))
            if action:
                return action
        for profile in profiles:
            for root in profile['read_roots']:
                action=self.unused(Action(tool='ssh.find_files',arguments={'target':s.target,'profile':profile['name'],'path':root,'patterns':[s.filename]},rationale='Public discovery exhausted; test existing authorized read-only SFTP access.'))
                if action:
                    return action
        if not profiles:
            self.limit('SSH unavailable: no matching configured profile. This limits SSH only; permitted public discovery was considered independently.')
        return None

    def observe(self,action,result,tid):
        s=self.state
        record={'tool_id':tid,'at':now(),'target':s.target,'run_id':self.ctx.rid,'tool':action.tool,'ok':result.ok,'summary':result.summary,
                'evidence':result.evidence[:16]}
        findings=[f for f in result.evidence if result.ok and ((f.get('kind')=='port' and f.get('state')=='open') or
                  (f.get('url') and f.get('status') is not None) or f.get('path'))]
        record['advances_investigation']=any(f.get('state')=='open' or f.get('path') or f.get('status') in {200,301,302,401,403} for f in findings)
        record['objective_achieved']=False
        s.verified_findings.extend({**f,'tool_id':tid,'target':s.target,'run_id':self.ctx.rid,'at':record['at']} for f in findings)
        s.verified_findings=s.verified_findings[-128:]
        s.attempts[action.key()]=record
        s.hypotheses.append({'candidate':action.arguments,'target':s.target,'run_id':self.ctx.rid,'at':record['at'],
                              'status':'observed' if record['advances_investigation'] else 'no_actionable_findings' if result.ok else 'not_verified',
                              'tool_id':tid,'summary':result.summary})
        s.hypotheses=s.hypotheses[-64:]
        if not result.ok:
            self.limit(action.tool+': '+result.summary)
        if result.ok and action.tool=='web.inspect':
            response=result.evidence[-1] if result.evidence else {}
            url=response.get('url',action.arguments['url'])
            if response.get('status')==200 and not result.truncated:
                if urlsplit(url).path.rstrip('/').endswith('/'+s.filename):
                    # A generic 200/soft-404 is not proof of a flag.
                    candidate=re.search(r'\b(?:THM|HTB|flag)\{[^\s{}]{1,200}\}',result.stdout,re.I)
                    if candidate or re.fullmatch(r'[a-fA-F0-9]{32}',result.stdout.strip()):
                        if s.objective_kind=='home_file':
                            self.limit('A public HTTP artifact is only a candidate; it does not prove retrieval from the requested user home directory.')
                        else:
                            s.result={'tool_id':tid,'at':now(),'target':s.target,'run_id':self.ctx.rid,'url':url,'content':result.stdout.strip()[:4096], 'verification':'Retrieved candidate; challenge acceptance is not independently verified.'}
                parser=Links()
                parser.feed(result.stdout[:32768])
                for link in parser.links:
                    candidate=urljoin(url,link)
                    if urlsplit(candidate).netloc==urlsplit(url).netloc and urlsplit(candidate).scheme==urlsplit(url).scheme and not urlsplit(candidate).fragment and len(urlsplit(candidate).path.split('/'))<=8:
                        if candidate not in s.candidates:
                            s.candidates.append(candidate)
        if result.ok and action.tool=='web.discover_paths':
            for f in result.evidence:
                if f.get('status')==200 and f.get('url'):
                    url=urljoin(f['url'].rstrip('/')+'/',s.filename)
                    if url not in s.candidates:
                        s.candidates.append(url)
        if result.ok and action.tool=='ssh.find_files':
            for f in result.evidence:
                if f.get('path','').rsplit('/',1)[-1]==s.filename:
                    s.remote_files.append({'target':s.target,'profile':action.arguments['profile'],'path':f['path']})
        if result.ok and action.tool=='ssh.read_file' and not result.truncated and result.stdout.strip():
            path=result.evidence[0].get('path','') if result.evidence else ''
            if path.rsplit('/',1)[-1]==s.filename and (s.objective_kind!='home_file' or path.startswith('/home/')):
                s.result={'tool_id':tid,'at':now(),'target':s.target,'run_id':self.ctx.rid,'path':path,'content':result.stdout[:4096], 'verification':'Retrieved file via SFTP; challenge acceptance is not verified.'}
        record['objective_achieved']=bool(s.result)
        s.candidates=s.candidates[:32]
        s.pending=None
        s.pending_steps=[]
        self.save()
        self.agent.event(self.ctx.sid,self.ctx.rid,'goal_observation',goal_id=s.id,observation=record,hypotheses=s.hypotheses[-1:])

    async def run(self,instruction):
        ctx=self.ctx
        saved=self.load(ctx.db,ctx.sid)
        if continuation(instruction):
            if not saved:
                self.agent.finish_answer(ctx.sid,ctx.rid,'No saved task goal to continue. State the goal and exact authorized target.')
                return 'needs_access'
            self.state=saved
        else:
            target,_=explicit_target(instruction)
            self.state=saved if saved and saved.instruction==instruction else TaskGoal(instruction=instruction,target=target)
            if saved and saved.target!=target:
                self.agent.event(ctx.sid,ctx.rid,'target_context_changed',previous_target=saved.target,target=target,reason='Prior target findings remain historical evidence, not current assumptions.')
        s=self.state
        ctx.intent='goal'
        ctx.remote_access_required=True
        if not s.target:
            targets=ctx.scope.data.get('allowed_targets',[])
            if len(targets)==1 and '/' not in targets[0]:
                s.target=targets[0]
        self.save()
        if not s.target:
            return self.checkpoint('Specify one exact authorized target; a CIDR is not expanded automatically.','needs_access')
        try:
            s.target=str(ipaddress.ip_address(s.target))
        except ValueError:
            return self.checkpoint('Autonomous room goals require one exact authorized target IP.','needs_access')
        ctx.run_target=s.target
        ctx.require_vpn=bool(re.search(r'\btryhackme\b',s.instruction,re.I))
        self.agent.plans[ctx.sid]['target']=s.target
        if re.search(r'\bhome\b',s.instruction,re.I) or '/home/' in s.instruction:
            s.objective_kind='home_file'
            s.completion_criteria=['Read the named file under /home/ using verified authorized file access','HTTP candidates do not prove home-directory provenance']
        await ctx.scope.destination(s.target)
        if s.scope_key!=scope_key(ctx.scope.data):
            s.attempts={}
            s.candidates=[]
            s.remote_files=[]
            s.pending=None
            s.result=None
            s.hypotheses=[]
            s.verified_findings=[]
            s.limitations=[]
            s.scope_key=scope_key(ctx.scope.data)
        s.status='active'
        self.save()
        for _ in range(ctx.settings.prefs.max_turns):
            self.transition('PLAN')
            if s.result:
                return self.complete()
            action=await self.next_action()
            s.pending=action
            s.pending_steps=[action] if action else []
            self.save()
            if action is None:
                return self.checkpoint('Current bounded discovery plan exhausted without a verified flag. Additional explicit evidence/access may be needed.','needs_access')
            if ctx.calls>=ctx.settings.prefs.max_tool_calls:
                return self.checkpoint('Tool-call budget reached')
            self.agent.event(ctx.sid,ctx.rid,'goal_plan',goal_id=s.id,action=action.model_dump(),decomposition=s.decomposition)
            self.transition('EXECUTE')
            result,tid=await self.agent.execute_tool(ctx,action.tool,action.arguments,rationale=action.rationale)
            self.transition('OBSERVE')
            if result.denied_reason:
                if 'budget' in result.denied_reason.lower():
                    return self.checkpoint(result.denied_reason)
                self.observe(action,result,tid)
                return self.checkpoint('Policy denied execution: '+result.denied_reason,'denied')
            self.observe(action,result,tid)
            self.transition('EVALUATE')
            if result.summary.startswith('Target preflight blocked:'):
                return self.checkpoint(result.summary,'needs_access')
            self.transition('REPLAN')
        if s.result:
            return self.complete()
        s.pending=await self.next_action()
        return self.checkpoint('Planner step budget reached')
