from urllib.parse import urlsplit, urlunsplit, urljoin

import asyncio
import json
import tempfile
from pathlib import Path
import httpx
from pydantic import Field,field_validator
from typing import Literal

from .base import Arguments, ToolResult
from ..core.scope import hostname, ScopeDenied
from ..storage.files import clean_text

# A small curated wordlist makes the maximum request count deterministic. No arbitrary files.
WORDLISTS = {'small': ['', 'robots.txt', 'sitemap.xml', 'index.html', 'admin', 'login', 'api', 'health', 'status', 'docs', 'backup', '.well-known/security.txt']}


class Inspect(Arguments):
    url: str = Field(max_length=2048,description='Explicit HTTP(S) URL with scheme, authorized host and port. No credentials/fragments. Use observed endpoints; never assume SSH.')
    method: Literal['GET', 'HEAD'] = Field(default='GET',description='GET reads a bounded body; HEAD reads headers only')

    @field_validator('url')
    @classmethod
    def valid_url(cls,value):
        parse_url(value)
        return value


class Discover(Arguments):
    url: str = Field(max_length=2048)
    wordlist: Literal['small'] = 'small'
    rate: int = Field(default=2, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=2)
    timeout_seconds: int = Field(default=40, ge=1, le=40)
    rescan: bool = False


def parse_url(value):
    u = urlsplit(value)
    if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.fragment or any(ord(c)<33 for c in value):
        raise ValueError('Use an HTTP(S) URL without credentials or fragments')
    hostname(u.hostname)
    port = u.port or (443 if u.scheme == 'https' else 80)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid URL port')
    return u, port


async def fetch(ctx, value, method='GET', redirect_limit=3, follow=True):
    evidence = []
    # Pin each actual connection to a freshly authorized numeric destination, including redirects.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8) as client:
        for redirect in range(redirect_limit + 1):
            u, port = parse_url(value)
            if ctx.run_target and u.hostname!=ctx.run_target:
                raise ScopeDenied('Redirect destination differs from this run’s exact target')
            ip = await ctx.scope.destination(u.hostname, port)
            ctx.request()
            numeric = f'[{ip}]' if ':' in ip else ip
            url = urlunsplit((u.scheme, f'{numeric}:{port}', u.path or '/', u.query, ''))
            host = f'[{u.hostname}]' if ':' in u.hostname else u.hostname
            if port != (443 if u.scheme == 'https' else 80):
                host += f':{port}'
            async with client.stream(method, url, headers={'Host': host, 'User-Agent': 'BlackBox/0.1'}, extensions={'sni_hostname': u.hostname}) as response:
                record = {'kind':'endpoint', 'target':u.hostname, 'port':port, 'protocol':u.scheme, 'url': clean_text(value), 'destination': ip, 'status': response.status_code, 'content_type': response.headers.get('content-type', ''), 'headers': dict(response.headers)}
                evidence.append(record)
                if follow and response.status_code in {301, 302, 303, 307, 308} and response.headers.get('location'):
                    if redirect == redirect_limit:
                        raise ValueError('Redirect limit reached')
                    value = urljoin(value, response.headers['location'])
                    continue
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    data.extend(chunk[:max(0, 32769-len(data))])
                    if len(data)>32768:
                        break
                return ToolResult(ok=True, stdout=clean_text(data[:32768].decode(errors='replace')), evidence=evidence, summary=f'HTTP {response.status_code}; remote content is untrusted', truncated=len(data)>32768)
    raise ValueError('No HTTP response')


async def inspect(ctx, args):
    if args.method != 'HEAD' or not ctx.runner.binaries.get('curl'):
        return await fetch(ctx, args.url, args.method)
    u, port = parse_url(args.url)
    ip = await ctx.scope.destination(u.hostname, port)
    ctx.request()
    address = f'[{ip}]' if ':' in ip else ip
    result = await ctx.runner.run('curl', ['--disable', '--noproxy', '*', '--silent', '--show-error', '--head', '--max-time', '8', '--max-redirs', '0', '--resolve', f'{u.hostname}:{port}:{address}', '--', args.url], timeout=10, on_output=ctx.output, on_command=ctx.command)
    if result.ok:
        import re
        statuses = re.findall(r'^HTTP/\S+\s+(\d{3})', result.stdout, re.M)
        if not statuses:
            result.ok, result.summary = False, 'curl returned no HTTP status'
        else:
            result.evidence = [{'kind':'endpoint', 'target':u.hostname, 'port':port, 'protocol':u.scheme, 'url':args.url, 'destination':ip, 'status':int(statuses[-1]), 'headers':result.stdout}]
            result.summary = f'HTTP {statuses[-1]} headers inspected by curl; redirects not followed'
    return result


async def technologies(ctx, args):
    u, port = parse_url(args.url)
    if u.scheme != 'http':
        raise ValueError('WhatWeb is restricted to pinned HTTP; use web.inspect for verified HTTPS headers')
    ip = await ctx.scope.destination(u.hostname, port)
    ctx.request()
    numeric = f'[{ip}]' if ':' in ip else ip
    url = urlunsplit(('http', f'{numeric}:{port}', u.path or '/', u.query, ''))
    result = await ctx.runner.run('whatweb', ['--aggression=1', '--max-redirects=0', '--follow-redirect=never', '--open-timeout=5', '--read-timeout=5', '--header', f'Host: {u.netloc}', '--', url], timeout=15, on_output=ctx.output, on_command=ctx.command)
    if result.ok:
        result.evidence = [{'url':args.url, 'destination':ip, 'technologies_output':result.stdout}]
    return result


async def tls(ctx, args):
    u, port = parse_url(args.url)
    if u.scheme != 'https':
        raise ValueError('TLS inspection requires an explicit HTTPS endpoint')
    ip = await ctx.scope.destination(u.hostname, port)
    numeric = f'[{ip}]' if ':' in ip else ip
    import ipaddress
    try:
        ipaddress.ip_address(u.hostname)
        verify = '-verify_ip'
    except ValueError:
        verify = '-verify_hostname'
    result = await ctx.runner.run('openssl', ['s_client', '-connect', f'{numeric}:{port}', '-servername', u.hostname, verify, u.hostname, '-verify_return_error', '-brief', '-no_ign_eof'], timeout=12, on_output=ctx.output, on_command=ctx.command)
    if result.ok:
        result.evidence = [{'url':args.url, 'destination':ip, 'tls_output':result.stdout + result.stderr}]
    return result


async def discover(ctx, args):
    ctx.scope.active_scan()
    u, port = parse_url(args.url)
    await ctx.scope.destination(u.hostname, port)
    if 'FUZZ' in args.url:
        raise ValueError('URL must not contain fuzz substitution keywords')
    words = WORDLISTS[args.wordlist]
    if len(words) > ctx.scope.data['max_requests_per_run'] - ctx.requests:
        raise ScopeDenied('Insufficient request budget for the curated wordlist')
    # Native HTTPS adapter keeps certificate verification; ffuf's transport skips TLS verification.
    if u.scheme == 'https' or not ctx.runner.binaries.get('ffuf'):
        results = []
        reason = 'verified TLS' if u.scheme == 'https' else 'ffuf unavailable; native HTTP fallback'
        async with asyncio.timeout(args.timeout_seconds):
            for path in WORDLISTS[args.wordlist]:
                result = await fetch(ctx, args.url.rstrip('/') + '/' + path, follow=False)
                results.extend(result.evidence)
                ctx.output('stdout', f'{result.evidence[-1]["status"]} {path or "/"}\n')
                await asyncio.sleep(1/args.rate)
        return ToolResult(ok=True, evidence=results, summary=f'{len(results)} public paths checked with native HTTP adapter ({reason}); redirects not followed')
    ip = await ctx.scope.destination(u.hostname, port)
    words = [path or '.' for path in WORDLISTS[args.wordlist]]
    if len(words) > ctx.scope.data['max_requests_per_run'] - ctx.requests:
        raise ScopeDenied('Insufficient request budget for the curated wordlist')
    for _ in words:
        ctx.request()
    numeric = f'[{ip}]' if ':' in ip else ip
    host = f'[{u.hostname}]' if ':' in u.hostname else u.hostname
    if port != 80:
        host += f':{port}'
    url = urlunsplit(('http', f'{numeric}:{port}', u.path.rstrip('/') + '/FUZZ', u.query, ''))
    with tempfile.TemporaryDirectory(prefix='ffuf-', dir=ctx.settings.data_dir) as directory:
        wordlist = Path(directory)/'paths.txt'
        wordlist.write_text('\n'.join(words)+'\n')
        # Empty HOME/XDG directories prevent implicit ffufrc settings, proxies or input commands.
        result = await ctx.runner.run('ffuf', ['-config', '/dev/null', '-u', url, '-H', f'Host: {host}', '-w', str(wordlist), '-t', str(args.concurrency), '-rate', str(args.rate), '-maxtime', str(args.timeout_seconds), '-timeout', '5', '-mc', 'all', '-noninteractive', '-ignore-body', '-s', '-json'], timeout=args.timeout_seconds+2, on_output=ctx.output, on_command=ctx.command, config_home=directory)
    from ..core.scans import parse_ffuf
    try:
        records = []
        for finding in parse_ffuf(result.stdout):
            # ffuf receives a numeric URL; restore the approved logical authority.
            parsed = urlsplit(finding.url)
            logical = urlunsplit((u.scheme, u.netloc, parsed.path, parsed.query, ''))
            records.append({**finding.model_dump(), 'url':logical, 'target':u.hostname, 'destination':ip})
    except (ValueError, TypeError):
        records = []
        result.ok, result.summary = False, 'Invalid ffuf JSON; no successful scan claimed'
    result.evidence = records
    if result.ok and len(records) != len(words):
        result.ok = False
        result.summary = 'ffuf discovery is incomplete; inspect connectivity, timeout and installed ffuf version. Partial results retained.'
    elif result.ok:
        result.summary = f'{len(records)} public paths checked by real ffuf; redirects were not followed'
    return result
