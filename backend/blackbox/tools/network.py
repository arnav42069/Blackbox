from pydantic import Field, field_validator

from .base import Arguments
from ..core.scope import hostname, ports, ScopeDenied


async def preflight(ctx,target):
    """Fixed, bounded route/ICMP checks inside the authorized tool's timeout.

    ICMP silence does not prove a host is down. A failed VPN/route check does
    prevent enumeration; neither the model nor remote content can override it.
    """
    import ipaddress
    import json
    import socket
    from .base import ToolResult
    ip=await ctx.scope.destination(target)
    address=ipaddress.ip_address(ip)
    interface=''
    if ctx.runner.binaries.get('ip'):
        route=await ctx.runner.run('ip',['-j','route','get',ip],timeout=3,on_command=ctx.command)
        try:
            entries=json.loads(route.stdout) if route.ok else []
            info=entries[0] if entries else {}
            interface=info.get('dev','')
            routed=bool(interface) and info.get('type') not in {'unreachable','blackhole','prohibit'}
        except (ValueError,TypeError,IndexError):
            routed=False
    else:
        try:
            # UDP connect selects a local route; it sends no packet.
            with socket.socket(socket.AF_INET6 if address.version==6 else socket.AF_INET,socket.SOCK_DGRAM) as sock:
                sock.connect((ip,9))
                routed=not ipaddress.ip_address(sock.getsockname()[0]).is_unspecified
        except OSError:
            routed=False
    vpn=address.is_loopback or interface.startswith(('tun','tap','wg','tailscale','ppp'))
    record={'kind':'route','target':target,'destination':ip,'interface':interface or 'unverified',
            'route_available':routed,'vpn_route_verified':vpn,'run_id':ctx.rid}
    if not routed or (ctx.require_vpn and not vpn):
        return ToolResult(ok=False,summary='Target preflight blocked: no verified VPN route to this TryHackMe target. Connect the authorized VPN or verify routing before continuing.' if routed else 'Target preflight blocked: no usable route to target.',evidence=[record])
    reach=await ping(ctx,Target(target=target))
    record['icmp_reachable']=True if reach.ok else None
    record['reachability_note']='ICMP reply observed' if reach.ok else 'ICMP unavailable or silent; reachability remains unverified. Bounded authorized TCP/HTTP checks may proceed.'
    return ToolResult(ok=True,summary=record['reachability_note'],evidence=[record])


class Empty(Arguments):
    pass


class Target(Arguments):
    target: str = Field(max_length=253)

    @field_validator('target')
    @classmethod
    def single_host(cls, value):
        return hostname(value)


class Scan(Target):
    ports: str = '1-1000'
    service_detection: bool = False
    rescan: bool = False

    @field_validator('ports')
    @classmethod
    def bounded_ports(cls, value):
        if len(ports(value)) > 1000:
            raise ValueError('At most 1000 ports per call')
        return value


async def ping(ctx, args):
    ip = await ctx.scope.destination(args.target)
    return await ctx.runner.run('ping', ['-n', '-c', '2', '-W', '2', ip], timeout=8, on_output=ctx.output, on_command=ctx.command)


async def scan(ctx, args):
    ctx.scope.active_scan()
    ip = await ctx.scope.destination(args.target)
    if not ports(args.ports) <= ports(ctx.scope.data['allowed_ports']):
        raise ScopeDenied('Requested scan ports are outside authorized scope')
    argv = ['-n', '-sT', '-Pn', '--max-rate', '20', '--max-retries', '1', '--host-timeout', '30s', '-p', args.ports]
    if ':' in ip:
        argv.append('-6')
    if args.service_detection:
        argv += ['-sV', '--version-light']
    from ..core.scans import parse_nmap
    # Human-readable stdout remains compatible; XML is a separate bounded artifact.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix='nmap-', dir=ctx.settings.data_dir) as directory:
        path = Path(directory)/'scan.xml'
        result = await ctx.runner.run('nmap', [*argv, '-oX', str(path), ip], timeout=35, on_output=ctx.output, on_command=ctx.command)
        if path.exists() and path.stat().st_size <= 65536:
            try:
                findings, complete = parse_nmap(path.read_text())
                result.evidence = [{**f.model_dump(), 'target':args.target, 'destination':f.target} for f in findings]
                if not complete:
                    result.ok = False
                    result.summary = 'Nmap output is incomplete; partial observations retained'
            except ValueError:
                result.ok = False
                result.summary = 'Invalid Nmap XML; no complete scan observation'
        elif result.ok:
            result.ok = False
            result.summary = 'Nmap did not produce bounded XML evidence'
        return result
