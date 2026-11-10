"""Deterministic intent routing. Plans describe actions; executors enforce policy."""
from __future__ import annotations

import re
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .scans import ScanStore, context_records
from .scope import hostname

Intent = Literal['http_discovery','ports','headers','technologies','tls','sql_injection','analyze','image','remote_access','general']


def classify(text: str) -> Intent:
    s = text.lower()
    image_request = re.search(r'\b(screenshot|image|diagram)\b', s)
    active_request = re.search(r'\b(host|tools|preflight|fuzz|fuzzing|discover open ports|scan ports|port scan|rescan|enumerate endpoints)\b', s)
    if image_request and not active_request:
        return 'image'
    if re.search(r'\b(analy[sz]e|parse|summari[sz]e)\b.*\b(scan|results|nmap|ffuf)\b', s):
        return 'analyze'
    if re.search(r'\b(sqlmap|sql injection|sqli)\b', s):
        return 'sql_injection'
    if re.search(r'\b(fuzz|fuzzing|ffuf|dirb|directory discovery|content discovery|endpoint enumeration|enumerate endpoints|identify http endpoints|discover endpoints)\b', s):
        return 'http_discovery'
    if re.search(r'\b(tls|ssl|openssl)\b', s):
        return 'tls'
    if re.search(r'\b(headers?|curl)\b', s):
        return 'headers'
    if re.search(r'\b(whatweb|web technologies|technology stack|identify technologies)\b', s):
        return 'technologies'
    if re.search(r'\b(nmap|open ports|port scan|scan ports|discover ports|rescan)\b', s):
        return 'ports'
    if re.search(r'\b(ssh|sftp|remote shell|remote host|profile|/home|flag)\b', s):
        return 'remote_access'
    return 'general'


class ExecutionConstraints(BaseModel):
    timeout_seconds: int
    rate: int
    concurrency: int
    request_budget: int
    max_tool_calls: int


class ExecutionPlan(BaseModel):
    intent: Intent
    target: str | None = None
    authorization_scope: dict
    previous_findings: list[dict] = Field(default_factory=list)
    tool: str | None = None
    rationale: str
    arguments: dict = Field(default_factory=dict)
    constraints: ExecutionConstraints
    expected_output: str = 'ToolResult with attributable structured evidence'
    state: Literal['ready','needs_target','unsupported','delegate','observe'] = 'delegate'
    rescan: bool = False


def explicit_target(text):
    urls = re.findall(r'https?://[^\s<>"\']+', text)
    if urls:
        from ..tools.web import parse_url
        url = urls[-1].rstrip('.,);')
        u, _ = parse_url(url)
        return u.hostname, url
    # Standalone numeric hosts never imply remote access.
    tokens = re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', text)
    if tokens:
        return hostname(tokens[-1]), None
    match = re.search(r'\b(?:on|for|at|target|host)\s+([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)\b', text)
    return (hostname(match[1]), None) if match else (None, None)


def http_candidates(previous):
    candidates, seen_requests, seen_ports = {}, set(), set()
    for row in previous:
        signature = (row['tool'], json.dumps(row['arguments'], sort_keys=True))
        if row['source']=='execution':
            if signature in seen_requests:
                continue
            seen_requests.add(signature)
        usable = row['freshness']=='fresh' and ((row['source']=='execution' and row['status']=='success' and row['complete']) or (row['source'] in {'screenshot','import'} and row['status'] in {'observed','imported'}))
        if not usable:
            continue
        for f in row['findings']:
            if f.get('kind')=='port':
                port_key = (f['target'], f.get('port'), f.get('protocol'))
                if port_key in seen_ports:
                    continue
                seen_ports.add(port_key)
            if f.get('url') and (f.get('kind')!='port' or f.get('state')=='open'):
                u = urlsplit(f['url'])
                if u.scheme not in {'http','https'}:
                    continue
                origin = f'{u.scheme}://{u.netloc}/'
                candidates.setdefault(origin, row)
    return candidates


class Planner:
    async def build(self, ctx, instruction):
        intent = classify(instruction)
        target, url = explicit_target(instruction)
        if not target and intent in {'http_discovery','ports','headers','technologies','tls'}:
            users = [m for m in ctx.db.messages(ctx.sid, 300) if m['role']=='user']
            if users and users[-1]['content']==instruction:
                users = users[:-1]
            for message in reversed(users):
                try:
                    prior_target, _ = explicit_target(message['content'])
                except ValueError:
                    continue
                if prior_target:
                    target = prior_target
                    break
        previous = await ScanStore(ctx.db).relevant(ctx, target)
        rescan = bool(re.search(r'\b(rescan|re-scan|force scan|fresh scan|scan again|ignore cache)\b', instruction, re.I))
        p = ctx.settings.prefs
        plan = ExecutionPlan(intent=intent, target=target, authorization_scope=ctx.scope.data, previous_findings=context_records(previous), rescan=rescan,
            rationale='Use the existing agent for a task requiring additional interpretation.', constraints=ExecutionConstraints(timeout_seconds=p.discovery_timeout_seconds, rate=p.http_rate, concurrency=p.http_concurrency, request_budget=max(0,ctx.scope.data.get('max_requests_per_run',0)-ctx.requests), max_tool_calls=p.max_tool_calls))
        if intent in {'general','remote_access'}:
            return plan
        if intent == 'sql_injection':
            plan.state, plan.rationale = 'unsupported', 'SQL injection automation is outside the current PRD action policy; sqlmap is not registered. No test was executed.'
            return plan
        if intent == 'image':
            plan.state, plan.tool, plan.rationale = 'observe', 'vision.observe', 'Use structured visual observations; do not initiate network access for an image-reading request.'
            return plan
        if intent == 'analyze':
            plan.state, plan.tool, plan.rationale = 'ready', 'scan.lookup', 'Analyze stored structured evidence without performing another network scan.'
            plan.arguments = {'target':target} if target else {}
            export = re.search(r'(?<![\w./])([A-Za-z0-9][A-Za-z0-9_./-]*\.(xml|json|txt))\b', instruction, re.I)
            if export:
                from ..tools.scans import Parse
                timestamp = re.search(r'\bobserved_at[=: ]+(\d+(?:\.\d+)?)', instruction)
                plan.tool = 'scan.parse'
                plan.arguments = Parse(path=export[1], format={'xml':'nmap_xml','json':'ffuf_json','txt':'terminal_text'}[export[2].lower()], observed_at=float(timestamp[1]) if timestamp else None).model_dump()
                plan.rationale = 'Parse the existing workspace export without network execution; unknown observation time is treated as stale.'
            return plan
        if intent in {'http_discovery','headers','technologies','tls'} and not url:
            candidates = http_candidates(previous)
            if len(candidates)==1:
                url, source = next(iter(candidates.items()))
                target = urlsplit(url).hostname
                plan.rationale = f"Reuse HTTP endpoint from {source['source']} evidence {source['id']} ({source['freshness']}); no Nmap or SSH prerequisite."
            elif len(candidates)>1:
                plan.state, plan.rationale = 'needs_target', 'Multiple HTTP endpoints match. Provide the exact authorized URL: ' + ', '.join(candidates)
                return plan
            else:
                # Infer only standard scheme/port from explicit web protocol wording.
                if target and re.search(r'\bhttps?\b', instruction, re.I):
                    scheme = 'https' if re.search(r'\bhttps\b', instruction, re.I) else 'http'
                    url = f'{scheme}://{target}/'
                    plan.rationale = 'Standard port inferred from the explicitly requested protocol; availability is not yet verified.'
                else:
                    plan.state, plan.rationale = 'needs_target', 'No fresh, usable HTTP endpoint is known. Supply a URL with scheme and port, or explicitly request a scoped port scan. Stale, incomplete or failed scans are not current observations.'
                    return plan
        if intent == 'ports' and not target:
            # A single previous target can resolve "scan again"; scope CIDRs are never scanned implicitly.
            targets = {f['target'] for row in previous for f in row['findings']}
            if len(targets)==1:
                target = targets.pop()
            else:
                plan.state, plan.rationale = 'needs_target', 'Specify one host and authorized ports for Nmap.'
                return plan
        plan.target = target
        if intent == 'ports':
            match = re.search(r'\bports?\s+(\d[\d,-]*)', instruction, re.I)
            # Choose only authorized ports, capped by the existing 1000-port schema.
            from .scope import ports
            allowed = sorted(ports(ctx.scope.data.get('allowed_ports','1-1000')))
            from .scope import port_expression
            selected=allowed[:1000]
            while len(port_expression(selected))>200:
                selected.pop()
            port_text = match[1] if match else port_expression(selected)
            plan.tool, plan.arguments = 'net.scan_ports', {'target':target, 'ports':port_text, 'rescan':rescan}
        else:
            tool = {'http_discovery':'web.discover_paths','headers':'web.inspect','technologies':'web.technologies','tls':'web.tls'}[intent]
            plan.tool, plan.arguments = tool, {'url':url}
            if intent == 'headers':
                plan.arguments['method'] = 'HEAD'
            if intent == 'http_discovery':
                plan.arguments.update(rate=p.http_rate, concurrency=p.http_concurrency, timeout_seconds=p.discovery_timeout_seconds, rescan=rescan)
        spec = ctx.agent.registry.specs.get(plan.tool)
        if not spec:
            plan.state, plan.rationale = 'unsupported', 'Requested tool is not registered.'
            return plan
        plan.arguments = spec.argument_schema.model_validate(plan.arguments).model_dump()
        if intent=='ports':
            plan.constraints.timeout_seconds, plan.constraints.rate, plan.constraints.concurrency = spec.timeout_s, 20, 1
            plan.expected_output = 'Nmap XML parsed into host, port and service evidence; human-readable stdout retained'
        elif intent=='http_discovery':
            plan.expected_output = 'ffuf JSON lines parsed into URLs and HTTP response codes; HTTPS native adapter returns equivalent evidence'
        else:
            plan.constraints.timeout_seconds = spec.timeout_s
        binary = spec.binary
        if intent=='http_discovery':
            binary = 'ffuf' if ctx.runner.binaries.get('ffuf') else None
        if intent=='headers':
            binary = 'curl' if ctx.runner.binaries.get('curl') else None
        if intent=='technologies' and not ctx.runner.binaries.get('whatweb'):
            plan.tool = 'web.inspect'
            plan.arguments = ctx.agent.registry.specs[plan.tool].argument_schema.model_validate({'url':url, 'method':'HEAD'}).model_dump()
            binary = 'curl' if ctx.runner.binaries.get('curl') else None
            plan.rationale = 'WhatWeb is unavailable; inspect HTTP headers as limited evidence. No technology fingerprint is claimed; SSH is unnecessary.'
        if binary and not ctx.runner.binaries.get(binary):
            # HTTPS content discovery uses the existing TLS-verifying native adapter.
            if not (intent=='http_discovery' and urlsplit(url).scheme=='https'):
                plan.state, plan.rationale = 'unsupported', f'Missing binary {binary}; install it and restart BlackBox. No scan executed.'
                return plan
        plan.state = 'ready'
        if plan.rationale.startswith('Use the existing'):
            plan.rationale = f'{intent} maps to {binary or plan.tool}; choose the least invasive registered protocol tool. SSH is unnecessary.'
        if intent=='http_discovery' and urlsplit(url).scheme=='https':
            plan.rationale += ' HTTPS uses the native adapter to preserve certificate verification; ffuf skips TLS verification.'
        if intent=='http_discovery' and not ctx.runner.binaries.get('ffuf'):
            plan.rationale += ' ffuf is unavailable; use bounded native HTTP discovery with the same scope, rate, timeout and request budget. SSH is unnecessary.'
        return plan
