from .scope import ScopeDenied


class PolicyEngine:
    """Model requests never grant permissions; executors independently pin destinations."""

    def authorize(self, ctx, spec, args=None):
        if spec.risk not in {'read_only', 'active_scan'}:
            raise ScopeDenied('This action class is forbidden')
        if spec.requires_target_scope:
            ctx.scope.check()
        if spec.risk == 'active_scan':
            ctx.scope.active_scan()
        if ctx.calls > ctx.settings.prefs.max_tool_calls:
            raise ScopeDenied('Tool call budget exhausted')

        if args is not None and spec.name == 'web.discover_paths':
            p = ctx.settings.prefs
            if args.rate > p.http_rate or args.concurrency > p.http_concurrency or args.timeout_seconds > p.discovery_timeout_seconds:
                raise ScopeDenied('Requested discovery limits exceed configured rate, concurrency or runtime')
        if spec.name.startswith('ssh.') and (getattr(ctx, 'remote_access_required', None) is False or getattr(ctx, 'intent', None) in {'http_discovery','ports','headers','technologies','tls','image','analyze','address_only'}):
            raise ScopeDenied('SSH is unnecessary for this intent; an IP address does not authorize remote shell access')

    async def validate_destination(self, ctx, spec, args):
        if not spec.requires_target_scope:
            return
        if hasattr(args, 'url'):
            from ..tools.web import parse_url
            u, port = parse_url(args.url)
            if ctx.run_target and u.hostname!=ctx.run_target:
                raise ScopeDenied('Destination differs from this run’s exact target')
            await ctx.scope.destination(u.hostname, port)
        elif hasattr(args, 'target') and args.target:
            if ctx.run_target and args.target!=ctx.run_target:
                raise ScopeDenied('Destination differs from this run’s exact target')
            await ctx.scope.destination(args.target)
            if spec.name == 'net.scan_ports':
                from .scope import ports
                if not ports(args.ports) <= ports(ctx.scope.data['allowed_ports']):
                    raise ScopeDenied('Requested scan ports are outside authorized scope')
