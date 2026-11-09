from pydantic import Field, field_validator

from .base import Arguments
from ..core.scope import hostname, ports, ScopeDenied


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
    return await ctx.runner.run('nmap', [*argv, ip], timeout=35, on_output=ctx.output, on_command=ctx.command)
