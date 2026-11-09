from __future__ import annotations

import ipaddress
import re
import socket
import time
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScopeDenied(ValueError):
    pass


def ports(value: str) -> set[int]:
    result = set()
    if not re.fullmatch(r"[0-9,-]{1,200}", value):
        raise ValueError("Use comma-separated ports or ranges")
    for part in value.split(','):
        bits = part.split('-')
        if len(bits) > 2:
            raise ValueError("Invalid port range")
        lo, hi = int(bits[0]), int(bits[-1])
        if not 1 <= lo <= hi <= 65535:
            raise ValueError("Ports must be 1–65535")
        result.update(range(lo, hi + 1))
    return result


def hostname(value: str) -> str:
    if len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]+", value) or value.startswith('-'):
        raise ValueError("Use a single IP address or DNS hostname")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value) or '..' in value:
            raise ValueError("Invalid hostname")
        return value.lower()


async def resolve(host: str) -> list[str]:
    import asyncio
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        answers = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = sorted({a[4][0] for a in answers})
        if not ips:
            raise ScopeDenied("DNS returned no addresses")
        return ips


class AuthorizationScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='Authorized lab', min_length=1, max_length=80)
    authorization_attested: bool
    allowed_targets: list[str] = Field(min_length=1, max_length=32)
    allowed_ports: str = '1-1000,8080,8443'
    allowed_remote_read_roots: list[str] = Field(default_factory=lambda: ['/home'], max_length=8)
    allow_active_scanning: bool = False
    expires_at: float | str = 'session_end'
    max_requests_per_run: int = Field(default=100, ge=1, le=500)

    @field_validator('authorization_attested')
    @classmethod
    def attested(cls, value):
        if not value:
            raise ValueError("Explicit authorization attestation is required")
        return value

    @field_validator('allowed_targets')
    @classmethod
    def targets(cls, values):
        for value in values:
            if '/' in value:
                ipaddress.ip_network(value, strict=False)
            else:
                hostname(value)
        return values

    @field_validator('allowed_ports')
    @classmethod
    def valid_ports(cls, value):
        ports(value)
        return value

    @field_validator('allowed_remote_read_roots')
    @classmethod
    def roots(cls, values):
        for value in values:
            p = PurePosixPath(value)
            # Lab-specific directories only. Never allow broad system/secret roots.
            if not p.is_absolute() or '..' in p.parts or len(p.parts) < 2 or p.parts[1] not in {'home', 'srv', 'opt', 'var', 'tmp'} or (p.parts[1] in {'var', 'tmp'} and len(p.parts) < 3):
                raise ValueError("Use /home or an explicit lab directory under /srv, /opt, /var or /tmp")
        return values

    @field_validator('expires_at')
    @classmethod
    def expiry(cls, value):
        if value == 'session_end':
            return value
        if not isinstance(value, (float, int)) or not time.time() < value <= time.time() + 86400:
            raise ValueError("Scope expiry must be within the next 24 hours")
        return value


async def approve_scope(scope: AuthorizationScope) -> dict:
    data = scope.model_dump()
    data['approved_dns'] = {}
    for value in scope.allowed_targets:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            name = hostname(value)
            data['approved_dns'][name] = await resolve(name)
    return data


class Scope:
    def __init__(self, data):
        self.data = data or {}

    def check(self):
        if not self.data.get('authorization_attested'):
            raise ScopeDenied("Scope required: attest authorization and add the exact target in Scope")
        expiry = self.data.get('expires_at', 'session_end')
        if expiry != 'session_end' and time.time() >= float(expiry):
            raise ScopeDenied("Session authorization has expired")

    async def destination(self, target: str, port: int | None = None) -> str:
        self.check()
        target = hostname(target)
        if port is not None and port not in ports(self.data['allowed_ports']):
            raise ScopeDenied(f"Port {port} is outside authorized scope")
        networks = []
        for value in self.data['allowed_targets']:
            try:
                networks.append(ipaddress.ip_network(value, strict=False))
            except ValueError:
                pass
        pinned = self.data.get('approved_dns', {}).get(target, [])
        addresses = await resolve(target)
        self.check()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_unspecified or ip.is_multicast or address == '169.254.169.254':
                raise ScopeDenied("Unspecified, multicast and cloud metadata destinations are forbidden")
            if not any(ip in n for n in networks) and address not in pinned:
                raise ScopeDenied(f"{target} resolves to {address}, outside authorized scope (or DNS changed)")
        return addresses[0]

    def active_scan(self):
        self.check()
        if not self.data.get('allow_active_scanning'):
            raise ScopeDenied("Active scanning is disabled in this scope")
