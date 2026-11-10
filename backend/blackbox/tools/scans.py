"""Internal result lookup and parsing of existing workspace scan exports."""
from typing import Literal
from pydantic import Field

from .base import Arguments, ToolResult
from .workspace import LocalPath, local_path
from ..core.scans import ScanStore, context_records, parse_nmap, parse_ffuf, parse_visible


class Lookup(Arguments):
    target: str | None = Field(default=None, max_length=253)


class Parse(LocalPath):
    format: Literal['nmap_xml','ffuf_json','terminal_text']
    observed_at: float | None = Field(default=None, ge=0)


async def lookup(ctx, args):
    records = await ScanStore(ctx.db).relevant(ctx, args.target)
    return ToolResult(ok=True, evidence=context_records(records, max_chars=24000), summary=f'{len(records)} scoped evidence records; freshness, status and source are explicit. No network scan executed.')


async def parse(ctx, args):
    path = local_path(ctx.settings.data_dir/'workspace', args.path)
    with path.open('rb') as stream:
        data = stream.read(65537)
    if len(data)>65536:
        raise ValueError('Scan export exceeds 64 KiB')
    text = data.decode('utf-8')
    complete = False
    if args.format=='nmap_xml':
        findings, complete = parse_nmap(text)
    elif args.format=='ffuf_json':
        findings = parse_ffuf(text)
    else:
        findings = parse_visible(text)
    from ..storage.db import now
    if args.observed_at is not None and args.observed_at > now():
        raise ValueError('Observation timestamp cannot be in the future')
    record_id = ScanStore(ctx.db).save(ctx.sid, ctx.scope.data, 'scan.parse', args.model_dump(), findings,
        source='import', status='imported', complete=complete, observed_at=args.observed_at if args.observed_at is not None else 0,
        evidence_id=ctx.tool_id, details={'path':args.path, 'verification':'Imported user-supplied output; not independently verified', 'timestamp_basis':'user-supplied observation time' if args.observed_at is not None else 'unknown; treated as stale'})
    return ToolResult(ok=True, evidence=[{'id':record_id, 'findings':[f.model_dump() for f in findings], 'source':'import', 'complete':complete, 'observed_at':args.observed_at, 'freshness':'unknown time; stale' if args.observed_at is None else ('fresh' if now()-args.observed_at <= ctx.settings.prefs.scan_freshness_seconds else 'stale')}], summary='Parsed existing output without running a scan. Imported output is unverified.')
