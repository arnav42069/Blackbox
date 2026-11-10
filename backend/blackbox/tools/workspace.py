from __future__ import annotations

import os
from itertools import islice
import re
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
import stat

from pydantic import Field, field_validator

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


def normalize_path(root: Path,value: str) -> str:
    """Only exact workspace-contained absolute paths have a safe relative repair."""
    if not value or '..' in PurePosixPath(value).parts or value.startswith('~') or '://' in value:
        raise ValueError('Workspace paths must be relative, without traversal. List the workspace and use an exact returned relative path.')
    if Path(value).is_absolute():
        try:
            value=str(Path(value).relative_to(root.absolute()))
        except ValueError:
            raise ValueError('Absolute paths outside the configured workspace are forbidden. workspace.read cannot read a remote host; use workspace.list to identify local relative paths.') from None
    safe_parts(value)
    return str(PurePosixPath(value))


@contextmanager
def open_local(root,value,*,directory=False):
    """Resolve confinement, then walk pinned descriptors without following races."""
    path=local_path(root,value)
    relative=path.relative_to(root.resolve()).parts
    descriptors=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        descriptors.append(fd)
        for i,part in enumerate(relative):
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
            if i<len(relative)-1 or directory:
                flags|=os.O_DIRECTORY
            fd=os.open(part,flags,dir_fd=fd)
            descriptors.append(fd)
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IsADirectoryError('workspace.read requires a regular file; list the workspace and select a file.')
        yield fd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class LocalPath(Arguments):
    path: str = Field(default='.', min_length=1,max_length=512,description='Relative to the application workspace, never the remote host. No traversal, credentials, or symlink escape. Use workspace.list first.',examples=['.','reports/scan.xml'])

    @field_validator('path')
    @classmethod
    def relative_path(cls,value):
        safe_parts(value)
        if Path(value).is_absolute() or '..' in PurePosixPath(value).parts or value.startswith('~') or '://' in value:
            raise ValueError('Workspace paths must be relative, without traversal')
        return str(PurePosixPath(value))


class Read(LocalPath):
    path: str = Field(min_length=1,max_length=512,description='Exact relative file path returned by workspace.list/search. Not a remote /home path.',examples=['notes.txt'])
    max_bytes: int = Field(default=8192, ge=1, le=32768)


class Search(LocalPath):
    pattern: str = Field(min_length=1, max_length=100)
    cap: int = Field(default=30, ge=1, le=100)


async def list_dir(ctx, args):
    entries = []
    with open_local(ctx.settings.data_dir/'workspace',args.path,directory=True) as fd:
        with os.scandir(fd) as listing:
            for p in sorted(islice(listing,200),key=lambda entry:entry.name):
                try:
                    safe_parts(p.name)
                    if not p.is_symlink():
                        entries.append({'name':p.name,'path':str(PurePosixPath(args.path)/p.name),'directory':p.is_dir(follow_symlinks=False)})
                except ValueError:
                    continue
    return ToolResult(ok=True, evidence=entries, summary=f'{len(entries)} workspace entries')


async def read(ctx, args):
    with open_local(ctx.settings.data_dir/'workspace',args.path) as fd:
        data=os.read(fd,args.max_bytes+1)
    return ToolResult(ok=True, stdout=clean_text(data[:args.max_bytes].decode(errors='replace')), truncated=len(data)>args.max_bytes, evidence=[{'path': args.path}])


async def search(ctx, args):
    root = local_path(ctx.settings.data_dir / 'workspace', args.path)
    if not root.is_dir():
        raise NotADirectoryError('workspace.search requires an existing workspace directory')
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
                with open_local(ctx.settings.data_dir/'workspace',rel) as fd:
                    text=os.read(fd,32768).decode(errors='replace')
                for i, line in enumerate(text.splitlines()):
                    if args.pattern.lower() in line.lower():
                        result.append({'path': rel, 'line': i+1, 'text': clean_text(line[:300])})
                        if len(result) >= args.cap:
                            break
            except (ValueError, OSError):
                continue
    return ToolResult(ok=True, evidence=result, summary=f'{len(result)} matching lines')
