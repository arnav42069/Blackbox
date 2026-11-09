from urllib.parse import urlsplit, urlunsplit, urljoin

import asyncio
import json
import tempfile
from pathlib import Path
import httpx
from pydantic import Field
from typing import Literal

from .base import Arguments, ToolResult
from ..core.scope import hostname, ScopeDenied
from ..storage.files import clean_text

# A small curated wordlist makes the maximum request count deterministic. No arbitrary files.
WORDLISTS = {'small': ['', 'robots.txt', 'sitemap.xml', 'index.html', 'admin', 'login', 'api', 'health', 'status', 'docs', 'backup', '.well-known/security.txt']}


class Inspect(Arguments):
    url: str = Field(max_length=2048)
    method: Literal['GET', 'HEAD'] = 'GET'


class Discover(Arguments):
    url: str = Field(max_length=2048)
    wordlist: Literal['small'] = 'small'
    rate: int = Field(default=2, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=2)


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
            ip = await ctx.scope.destination(u.hostname, port)
            ctx.request()
            numeric = f'[{ip}]' if ':' in ip else ip
            url = urlunsplit((u.scheme, f'{numeric}:{port}', u.path or '/', u.query, ''))
            host = f'[{u.hostname}]' if ':' in u.hostname else u.hostname
            if port != (443 if u.scheme == 'https' else 80):
                host += f':{port}'
            async with client.stream(method, url, headers={'Host': host, 'User-Agent': 'BlackBox/0.1'}, extensions={'sni_hostname': u.hostname}) as response:
                record = {'url': clean_text(value), 'destination': ip, 'status': response.status_code, 'content_type': response.headers.get('content-type', '')}
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
    return await fetch(ctx, args.url, args.method)


async def discover(ctx, args):
    ctx.scope.active_scan()
    u, port = parse_url(args.url)
    await ctx.scope.destination(u.hostname, port)
    if 'FUZZ' in args.url:
        raise ValueError('URL must not contain fuzz substitution keywords')
    # Native HTTPS adapter keeps certificate verification; ffuf's transport skips TLS verification.
    if u.scheme == 'https':
        results = []
        for path in WORDLISTS[args.wordlist]:
            result = await fetch(ctx, args.url.rstrip('/') + '/' + path, follow=False)
            results.extend(result.evidence)
            ctx.output('stdout', f'{result.evidence[-1]["status"]} {path or "/"}\n')
            await asyncio.sleep(1/args.rate)
        return ToolResult(ok=True, evidence=results, summary=f'{len(results)} public HTTPS paths checked with verified TLS')
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
        result = await ctx.runner.run('ffuf', ['-config', '/dev/null', '-u', url, '-H', f'Host: {host}', '-w', str(wordlist), '-t', str(args.concurrency), '-rate', str(args.rate), '-maxtime', '40', '-timeout', '5', '-mc', 'all', '-noninteractive', '-ignore-body', '-s', '-json'], timeout=45, on_output=ctx.output, on_command=ctx.command, config_home=directory)
    records = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
            records.append({'url': record.get('url'), 'status': record.get('status'), 'destination': ip})
        except ValueError:
            continue
    result.evidence = records
    if result.ok and not records:
        result.ok = False
        result.summary = 'ffuf returned no HTTP results; inspect connectivity and installed ffuf version'
    elif result.ok:
        result.summary = f'{len(records)} public paths checked by real ffuf; redirects were not followed'
    return result
