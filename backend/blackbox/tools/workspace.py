from __future__ import annotations

import os
from itertools import islice
import re
from pathlib import Path, PurePosixPath

from pydantic import Field

from .base import Arguments, ToolResult
from ..storage.files import clean_text


SECRET_NAMES = {'.ssh', '.gnupg', '.aws', '.azure', '.config', '.env', '.git', '.kube', '.local', '.cache', 'credentials', 'shadow', 'passwd', 'authorized_keys', 'known_hosts', '.netrc', '.npmrc', '.pypirc', '.bash_history', '.zsh_history'}


def safe_parts(path: str):
    p = PurePosixPath(path)
    if any((part.startswith('.') and part != '.') or part in SECRET_NAMES or part.startswith('.env') or re.search(r'(?i)(id_(rsa|ed25519|ecdsa)|\.(pem|key|p12|pfx|kdbx)$|cookie|token|secret|password|credential)', part) for part in p.parts):
        raise ValueError('Credential and secret stores are forbidden')
    if any(ord(c) < 32 for c in path) or '\\' in path:
        raise ValueError('Invalid path')


def local_path(root: Path, value: str) -> Path:
    safe_parts(value)
    if Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Workspace paths must be relative, without traversal')
    result = (root / value).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError('Path escapes workspace')
    safe_parts(str(result.relative_to(root.resolve())))
    return result


class LocalPath(Arguments):
    path: str = Field(default='.', max_length=512)


class Read(LocalPath):
    max_bytes: int = Field(default=8192, ge=1, le=32768)


class Search(LocalPath):
    pattern: str = Field(min_length=1, max_length=100)
    cap: int = Field(default=30, ge=1, le=100)


async def list_dir(ctx, args):
    path = local_path(ctx.settings.data_dir / 'workspace', args.path)
    entries = []
    for p in sorted(islice(path.iterdir(), 200)):
        try:
            local_path(ctx.settings.data_dir / 'workspace', str(p.relative_to(ctx.settings.data_dir / 'workspace')))
            entries.append({'name': p.name, 'directory': p.is_dir()})
        except ValueError:
            continue
    return ToolResult(ok=True, evidence=entries, summary=f'{len(entries)} workspace entries')


async def read(ctx, args):
    p = local_path(ctx.settings.data_dir / 'workspace', args.path)
    if not p.is_file():
        raise ValueError('Not a regular workspace file')
    with p.open('rb') as f:
        data = f.read(args.max_bytes + 1)
    return ToolResult(ok=True, stdout=clean_text(data[:args.max_bytes].decode(errors='replace')), truncated=len(data)>args.max_bytes, evidence=[{'path': args.path}])


async def search(ctx, args):
    root = local_path(ctx.settings.data_dir / 'workspace', args.path)
    result, inspected = [], 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith('.') and not (Path(current)/d).is_symlink()][:50]
        if len(Path(current).relative_to(root).parts) >= 5:
            dirs.clear()
        for name in sorted(files):
            inspected += 1
            if inspected > 200 or len(result) >= args.cap:
                return ToolResult(ok=True, evidence=result, summary='Workspace search reached its cap', truncated=True)
            try:
                rel = str((Path(current)/name).relative_to(ctx.settings.data_dir / 'workspace'))
                p = local_path(ctx.settings.data_dir / 'workspace', rel)
                if p.is_symlink() or not p.is_file():
                    continue
                with p.open('rb') as f:
                    text = f.read(32768).decode(errors='replace')
                for i, line in enumerate(text.splitlines()):
                    if args.pattern.lower() in line.lower():
                        result.append({'path': rel, 'line': i+1, 'text': clean_text(line[:300])})
                        if len(result) >= args.cap:
                            break
            except (ValueError, OSError):
                continue
    return ToolResult(ok=True, evidence=result, summary=f'{len(result)} matching lines')
