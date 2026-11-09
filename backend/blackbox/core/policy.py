from .scope import ScopeDenied


class PolicyEngine:
    """Model requests never grant permissions; executors independently pin destinations."""

    def authorize(self, ctx, spec):
        if spec.risk not in {'read_only', 'active_scan'}:
            raise ScopeDenied('This action class is forbidden')
        if spec.requires_target_scope:
            ctx.scope.check()
        if spec.risk == 'active_scan':
            ctx.scope.active_scan()
        if ctx.calls > ctx.settings.prefs.max_tool_calls:
            raise ScopeDenied('Tool call budget exhausted')
