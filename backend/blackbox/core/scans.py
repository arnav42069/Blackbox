"""Bounded parsers and session-local evidence retrieval. No parser grants scope."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ..storage.db import now, uid
from ..storage.files import clean_value


class Finding(BaseModel):
    kind: str
    target: str = ''
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: str = ''
    service: str = ''
    product: str = ''
    version: str = ''
    url: str = ''
    status: int | None = None
    state: str = ''
    quote: str = ''
    url_inferred_from_service: bool = False


def endpoint(target, port, service='', tunnel=''):
    # Only explicitly identified HTTP services imply a scheme, not arbitrary open ports.
    if 'http' not in service.lower():
        return ''
    host = f'[{target}]' if ':' in target else target
    return f'{"https" if tunnel == "ssl" or "https" in service.lower() else "http"}://{host}:{port}/'


def parse_nmap(text):
    if '<!ENTITY' in text.upper() or len(text) > 65536:
        raise ValueError('Unsupported or oversized Nmap XML')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError('Invalid Nmap XML') from e
    if root.tag != 'nmaprun':
        raise ValueError('Expected Nmap XML')
    findings = []
    for host in root.findall('host'):
        address = host.find('address')
        if address is None:
            continue
        target = str(ipaddress.ip_address(address.get('addr')))
        findings.append(Finding(kind='host', target=target, state=host.findtext('status') or (host.find('status').get('state', '') if host.find('status') is not None else '')))
        for entry in host.findall('ports/port'):
            state = entry.find('state')
            service = entry.find('service')
            name = service.get('name', '') if service is not None else ''
            tunnel = service.get('tunnel', '') if service is not None else ''
            port = int(entry.get('portid'))
            findings.append(Finding(kind='port', target=target, port=port, protocol=entry.get('protocol', ''), state=state.get('state', '') if state is not None else '', service=name, product=service.get('product','') if service is not None else '',version=service.get('version','') if service is not None else '',url=endpoint(target, port, name, tunnel) if entry.get('protocol')=='tcp' else '', url_inferred_from_service=True))
    finished = root.find('runstats/finished')
    return findings, finished is not None and finished.get('exit') == 'success'


def parse_ffuf(text):
    try:
        data = json.loads(text)
        records = data.get('results', [data]) if isinstance(data, dict) else data
    except ValueError:
        records = [json.loads(line) for line in text.splitlines() if line.strip().startswith('{')]
    findings = []
    for record in records:
        if not isinstance(record, dict) or not record.get('url') or not isinstance(record.get('status'), int):
            continue
        u = urlsplit(record['url'])
        if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password:
            continue
        findings.append(Finding(kind='endpoint', target=u.hostname, port=u.port or (443 if u.scheme == 'https' else 80), protocol=u.scheme, url=record['url'], status=record['status']))
    return findings


def parse_visible(text):
    """Extract only literal text. Screenshot rows remain unverified visual observations."""
    findings, target = [], ''
    for line in text.splitlines():
        report = re.search(r'Nmap scan report for (.+)', line)
        if report:
            candidate = report[1].strip().split()[-1].strip('()')
            try:
                target = str(ipaddress.ip_address(candidate))
            except ValueError:
                target = ''
        row = re.match(r'\s*(\d{1,5})/(tcp|udp)\s+(open|closed|filtered|open\|filtered)\s+(\S+)', line)
        if row and target and 1 <= int(row[1]) <= 65535:
            findings.append(Finding(kind='port', target=target, port=int(row[1]), protocol=row[2], state=row[3], service=row[4], url=endpoint(target, int(row[1]), row[4]) if row[3]=='open' and row[2]=='tcp' else '', quote=line, url_inferred_from_service=True))
        for url in re.findall(r'https?://[^\s<>"\']+', line):
            url = url.rstrip('.,);')
            try:
                u = urlsplit(url)
                if u.hostname and not u.username and not u.password:
                    findings.append(Finding(kind='endpoint', target=u.hostname, port=u.port or (443 if u.scheme=='https' else 80), protocol=u.scheme, url=url, quote=line))
            except ValueError:
                pass
        for address, port in re.findall(r'(?<![\w.])((?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})(?!\d)', line):
            try:
                findings.append(Finding(kind='port', target=str(ipaddress.ip_address(address)), port=int(port), quote=line))
            except ValueError:
                pass
        for ip in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', line):
            try:
                findings.append(Finding(kind='host', target=str(ipaddress.ip_address(ip)), quote=line))
            except ValueError:
                pass
        if re.search(r'error|exception|failed|refused|timed out|HTTP/\S+\s+\d{3}', line, re.I):
            status = re.search(r'HTTP/\S+\s+(\d{3})', line)
            findings.append(Finding(kind='http_status' if status else 'error', status=int(status[1]) if status else None, quote=line))
    return findings[:256]


def normalized_args(tool, args):
    result = {k: v for k, v in args.items() if k != 'rescan'}
    if isinstance(result.get('target'),str):
        result['target'] = result['target'].lower().rstrip('.')
    if tool == 'net.scan_ports':
        from .scope import ports
        result['ports'] = ','.join(map(str, sorted(ports(result.get('ports', '1-1000')))))
    if 'url' in result:
        u = urlsplit(result['url'])
        port = u.port or (443 if u.scheme=='https' else 80)
        host = f'[{u.hostname}]' if ':' in (u.hostname or '') else u.hostname
        result['url'] = urlunsplit((u.scheme.lower(), f'{host}:{port}', u.path or '/', u.query, ''))
    return result


def scope_key(scope):
    # Changes to authorization (including DNS pins/expiry) invalidate cache identity.
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


class ScanStore:
    def __init__(self, db):
        self.db = db

    def save(self, sid, scope, tool, args, findings, *, status='success', complete=True, source='execution', evidence_id='', observed_at=None, details=None):
        record_id = uid()
        args = clean_value(args)
        findings = clean_value([f.model_dump() if isinstance(f, Finding) else f for f in findings])
        details = clean_value(details or {})
        stamp = now() if observed_at is None else observed_at
        try:
            key = json.dumps(normalized_args(tool, args), sort_keys=True)
        except (ValueError, TypeError, AttributeError):
            key = json.dumps(args, sort_keys=True)
        target = args.get('target', args.get('url', ''))
        target = target if isinstance(target, str) else ''
        self.db.execute('INSERT INTO scan_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (record_id, sid, scope_key(scope), tool, key, target, stamp, now(), status, int(complete), source, evidence_id, json.dumps(findings), json.dumps(details)))
        self.db.execute('DELETE FROM scan_results WHERE session_id=? AND id NOT IN (SELECT id FROM scan_results WHERE session_id=? ORDER BY stored_at DESC LIMIT 1000)', (sid, sid))
        return record_id

    def import_legacy(self, ctx):
        # Older versions retained raw journals only. Preserve their original timestamps
        # but never claim that the old journal was bound to the current authorization.
        rows = ctx.db.rows("""SELECT t.* FROM tool_events t JOIN runs r ON r.id=t.run_id
            WHERE r.session_id=? AND t.ended_at IS NOT NULL
            AND t.tool_name IN ('net.scan_ports','web.discover_paths','web.inspect')
            AND NOT EXISTS (SELECT 1 FROM scan_results s WHERE s.evidence_id=t.id)
            ORDER BY t.started_at DESC LIMIT 200""", (ctx.sid,))
        for row in rows:
            try:
                result, args = json.loads(row['output_json']), json.loads(row['args_json'])
                if row['tool_name']=='net.scan_ports':
                    findings = parse_visible(result.get('stdout', ''))
                else:
                    findings = parse_ffuf(json.dumps({'results':result.get('evidence',[])}))
                self.save(ctx.sid, ctx.scope.data, row['tool_name'], args, findings,
                    status='imported' if row['status']=='success' else row['status'],
                    complete=False, source='import', evidence_id=row['id'], observed_at=row['started_at'],
                    details={'verification':'Legacy journal import; original scope binding and completeness are unknown. Not a live scan or executable scan cache.'})
            except (ValueError, TypeError, KeyError, AttributeError):
                continue

    async def relevant(self, ctx, target=None):
        self.import_legacy(ctx)
        aliases = {target} if target else set()
        if target:
            try:
                aliases.add(await ctx.scope.destination(target))
            except (ValueError, OSError):
                return []
        rows = ctx.db.rows("SELECT * FROM scan_results WHERE session_id=? AND (scope_key=? OR source IN ('screenshot','import')) ORDER BY observed_at DESC LIMIT 200", (ctx.sid, scope_key(ctx.scope.data)))
        result = []
        for row in rows:
            findings = []
            for f in json.loads(row['findings_json']):
                if target and f.get('target') not in aliases and f.get('destination') not in aliases:
                    continue
                if not f.get('target'):
                    continue
                try:
                    await ctx.scope.destination(f['target'], f.get('port'))
                except (ValueError, OSError):
                    continue
                findings.append(f)
            if not findings:
                # Failed/empty scans remain inspectable, without becoming discoveries.
                raw = row['target']
                candidate = urlsplit(raw).hostname if '://' in raw else raw
                if not candidate or (target and candidate not in aliases):
                    continue
                try:
                    await ctx.scope.destination(candidate)
                except (ValueError, OSError):
                    continue
            age = now()-row['observed_at']
            freshness = 'fresh' if 0 <= age <= ctx.settings.prefs.scan_freshness_seconds else 'stale'
            result.append({**{k:row[k] for k in ('id','tool','status','complete','source','evidence_id','observed_at')}, 'arguments':json.loads(row['args_json']), 'freshness':freshness, 'findings':findings, 'details':{k:v for k,v in json.loads(row['details_json']).items() if k!='result'}})
        return result

    async def cached(self, ctx, tool, args):
        if args.get('rescan'):
            return None
        key = normalized_args(tool, args)
        rows = await self.relevant(ctx, args.get('target'))
        for row in rows:
            if row['tool'] != tool or row['source'] != 'execution' or row['arguments'] != key:
                continue
            # A newer failed/incomplete attempt invalidates an older equivalent success.
            if row['status'] != 'success' or not row['complete'] or row['freshness'] != 'fresh':
                return None
            stored = self.db.one('SELECT details_json FROM scan_results WHERE id=?', (row['id'],))
            return row, json.loads(stored['details_json'])['result']
        return None


def context_records(records, max_chars=16000):
    """Bound planning/model context; full findings remain in scan_results."""
    result, size = [], 0
    for row in records[:16]:
        # Preserve protocol/HTTP evidence before common closed-port rows.
        findings = sorted(row['findings'], key=lambda f: (not bool(f.get('url')), f.get('state')!='open'))[:48]
        record = {**row, 'findings':findings, 'findings_omitted':len(row['findings'])-len(findings)}
        cost = len(json.dumps(record))
        if size+cost > max_chars:
            record = {**record, 'findings':findings[:4], 'findings_omitted':len(row['findings'])-len(findings[:4])}
            cost = len(json.dumps(record))
        if size+cost > max_chars:
            break
        result.append(record)
        size += cost
    return result
