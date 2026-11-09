import time

import pytest
from pydantic import ValidationError

from blackbox.core.scope import AuthorizationScope, Scope, ScopeDenied, approve_scope, ports, hostname


def scope(**kw):
    return AuthorizationScope(authorization_attested=True, allowed_targets=['127.0.0.1/32'], **kw)


@pytest.mark.parametrize('value', ['0', '65536', '2-1', '22;whoami', '1,,2', '-p22', '1-2-3'])
def test_invalid_ports(value):
    with pytest.raises(ValueError):
        ports(value)


@pytest.mark.parametrize('value', ['1.2.3.4;id', '-oProxyCommand=sh', 'host\nlocalhost', 'http://127.0.0.1', '../../host', 'lab..example'])
def test_single_target(value):
    with pytest.raises(ValueError):
        hostname(value)


async def test_scope_ip_port_expiry():
    s = Scope(await approve_scope(scope(allowed_ports='22,80-82')))
    assert await s.destination('127.0.0.1', 80) == '127.0.0.1'
    for target, port in [('127.0.0.2', 80), ('8.8.8.8', 80), ('127.0.0.1', 443)]:
        with pytest.raises(ScopeDenied):
            await s.destination(target, port)
    with pytest.raises(ScopeDenied):
        s.active_scan()
    s.data['expires_at'] = time.time()-1
    with pytest.raises(ScopeDenied):
        await s.destination('127.0.0.1')


async def test_dns_rebinding(monkeypatch):
    import blackbox.core.scope as module
    async def first(host):
        return ['127.0.0.1']
    monkeypatch.setattr(module, 'resolve', first)
    s = Scope(await approve_scope(AuthorizationScope(authorization_attested=True, allowed_targets=['lab.test'])))
    assert await s.destination('lab.test') == '127.0.0.1'
    async def rebound(host):
        return ['8.8.8.8']
    monkeypatch.setattr(module, 'resolve', rebound)
    with pytest.raises(ScopeDenied):
        await s.destination('lab.test')


@pytest.mark.parametrize('root', ['/', '/etc', '/root', '/home/../etc', '/tmp', '/var'])
def test_forbidden_roots(root):
    with pytest.raises(ValidationError):
        scope(allowed_remote_read_roots=[root])


async def test_no_private_ip_auto_authorization():
    with pytest.raises(ScopeDenied):
        await Scope(None).destination('10.48.158.17')
    with pytest.raises(ValidationError):
        AuthorizationScope(authorization_attested=False, allowed_targets=['10.0.0.1'])


async def test_ipv6():
    s = Scope(await approve_scope(AuthorizationScope(authorization_attested=True, allowed_targets=['::1/128'])))
    assert await s.destination('::1') == '::1'


async def test_expiration_during_dns_is_denied(monkeypatch):
    import blackbox.core.scope as module
    s=Scope(await approve_scope(scope()))
    async def expired(host):
        s.data['expires_at']=time.time()-1
        return ['127.0.0.1']
    monkeypatch.setattr(module,'resolve',expired)
    with pytest.raises(ScopeDenied):
        await s.destination('127.0.0.1')
