from __future__ import annotations

import fnmatch
import os
import re
import stat
from pathlib import Path, PurePosixPath
from contextlib import asynccontextmanager

import asyncssh
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import ToolResult
from .network import Target
from .workspace import safe_parts
from ..core.scope import ScopeDenied
from ..storage.files import clean_text


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=r'^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$')
    key_path: str | None = None
    known_hosts: str = Field(default_factory=lambda: str(Path.home()/'.ssh/known_hosts'))
    allowed_read_roots: list[str] = Field(default_factory=lambda: ['/home'])


def profiles(settings):
    path = settings.config_dir / 'ssh_profiles.yaml'
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ValueError('ssh_profiles.yaml must have permissions 0600')
    data = yaml.safe_load(path.read_text()) or {}
    return {name: Profile.model_validate(body) for name, body in data.items()}


def public_profiles(settings):
    return [{'name': name, 'host': p.host, 'port': p.port, 'read_roots': p.allowed_read_roots} for name, p in profiles(settings).items()]


class SSHArgs(Target):
    profile: str = Field(min_length=1, max_length=80)


class RemotePath(SSHArgs):
    path: str = Field(default='/home', max_length=512)


class Find(RemotePath):
    patterns: list[str] = Field(default_factory=lambda: ['*flag*', 'user.txt'], min_length=1, max_length=5)
    max_depth: int = Field(default=3, ge=0, le=5)
    max_results: int = Field(default=30, ge=1, le=100)

    @field_validator('patterns')
    @classmethod
    def pattern(cls, values):
        if any(not re.fullmatch(r'[a-zA-Z0-9_.*?\[\]-]{1,80}', x) for x in values):
            raise ValueError('Use simple filename patterns only')
        return values


class RemoteRead(RemotePath):
    max_bytes: int = Field(default=4096, ge=1, le=16384)


def under(path, roots):
    p = PurePosixPath(path)
    return any(p == PurePosixPath(root) or PurePosixPath(root) in p.parents for root in roots)


def allowed(ctx, p, path):
    safe_parts(path)
    if not path.startswith('/') or '..' in PurePosixPath(path).parts or not under(path, ctx.scope.data['allowed_remote_read_roots']) or not under(path, p.allowed_read_roots):
        raise ScopeDenied('Remote path is outside approved read roots')


@asynccontextmanager
async def connection(ctx, args):
    p = profiles(ctx.settings).get(args.profile)
    if not p:
        raise ValueError('Needs access: configure an SSH profile with an existing key or SSH agent')
    if args.target.lower() != p.host.lower():
        raise ScopeDenied('Target must match the configured SSH profile host')
    ip = await ctx.scope.destination(args.target, p.port)
    keys = []
    if p.key_path:
        key = Path(p.key_path).expanduser()
        if not key.is_file() or key.stat().st_mode & 0o077:
            raise ValueError('SSH key reference must be a private regular file (0600)')
        keys = [str(key)]
    if not keys and not os.environ.get('SSH_AUTH_SOCK'):
        raise ValueError('Needs access: no configured SSH key or SSH agent')
    if not Path(p.known_hosts).expanduser().is_file():
        raise ValueError('Needs access: known_hosts missing; verify the host fingerprint using SSH first')
    # Fixed numeric endpoint prevents DNS rebinding; alias preserves original host-key identity.
    try:
        async with asyncssh.connect(ip, config=None, gss_host=None, gss_auth=False, gss_kex=False, proxy_command=None, tunnel=None, canonicalize_hostname=False, agent_forwarding=False, send_env=[], port=p.port, username=p.username, client_keys=keys, agent_path=os.environ.get('SSH_AUTH_SOCK'), known_hosts=str(Path(p.known_hosts).expanduser()), host_key_alias=p.host, password=None, preferred_auth=['publickey'], connect_timeout=5, login_timeout=8) as conn:
            async with conn.start_sftp_client() as sftp:
                yield sftp, p
    except asyncssh.HostKeyNotVerifiable as e:
        raise ValueError('Needs access: SSH host key verification failed; verify the fingerprint before updating known_hosts') from e
    except asyncssh.PermissionDenied as e:
        raise ValueError('Needs access: SSH public-key authentication denied') from e


async def checked_path(ctx, p, sftp, path):
    allowed(ctx, p, path)
    canonical = str(await sftp.realpath(path))
    allowed(ctx, p, canonical)
    return canonical


async def probe(ctx, args):
    async with connection(ctx, args):
        return ToolResult(ok=True, summary='Authorized SSH public-key access and SFTP verified', evidence=[{'target': args.target, 'profile': args.profile}])


async def list_dir(ctx, args):
    async with connection(ctx, args) as (sftp, p):
        path = await checked_path(ctx, p, sftp, args.path)
        result = []
        async for entry in sftp.scandir(path):
            if len(result) >= 100:
                break
            if entry.filename in {'.', '..'}:
                continue
            try:
                child = await checked_path(ctx, p, sftp, str(PurePosixPath(path)/entry.filename))
                result.append({'path': child, 'directory': stat.S_ISDIR(entry.attrs.permissions or 0)})
            except (ValueError, asyncssh.SFTPError):
                continue
        return ToolResult(ok=True, evidence=result, summary=f'{len(result)} approved entries', truncated=len(result)>=100)


async def find_files(ctx, args):
    async with connection(ctx, args) as (sftp, p):
        root = await checked_path(ctx, p, sftp, args.path)
        queue, visited, result, inspected = [(root, 0)], set(), [], 0
        while queue and inspected < 500 and len(result) < args.max_results:
            path, depth = queue.pop(0)
            if path in visited:
                continue
            visited.add(path)
            try:
                async for entry in sftp.scandir(path):
                    inspected += 1
                    if inspected > 500:
                        break
                    if entry.filename in {'.', '..'}:
                        continue
                    try:
                        child = await checked_path(ctx, p, sftp, str(PurePosixPath(path)/entry.filename))
                        attrs = await sftp.stat(child)
                        mode = attrs.permissions or 0
                        if stat.S_ISDIR(mode) and depth < args.max_depth:
                            queue.append((child, depth+1))
                        elif stat.S_ISREG(mode) and any(fnmatch.fnmatch(entry.filename, pattern) for pattern in args.patterns):
                            result.append({'path': child, 'size': attrs.size})
                            if len(result) >= args.max_results:
                                break
                    except (ValueError, asyncssh.SFTPError):
                        continue
            except asyncssh.SFTPError:
                continue
        return ToolResult(ok=True, evidence=result, summary=f'{len(result)} matching authorized files', truncated=inspected>=500 or len(result)>=args.max_results)


async def read_file(ctx, args):
    async with connection(ctx, args) as (sftp, p):
        path = await checked_path(ctx, p, sftp, args.path)
        attrs = await sftp.stat(path)
        if not stat.S_ISREG(attrs.permissions or 0):
            raise ValueError('Only regular files can be read')
        async with sftp.open(path, 'rb', encoding=None) as f:
            data = await f.read(args.max_bytes+1)
        return ToolResult(ok=True, stdout=clean_text(data[:args.max_bytes].decode(errors='replace')), evidence=[{'target': args.target, 'path': path}], truncated=len(data)>args.max_bytes, summary='Read-only authorized SFTP file evidence')
