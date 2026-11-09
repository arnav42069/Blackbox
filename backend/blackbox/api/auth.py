from __future__ import annotations

import secrets
import time

from starlette.responses import JSONResponse


class Auth:
    def __init__(self):
        self.bearer = secrets.token_urlsafe(48)
        self.pairing = secrets.token_urlsafe(32)
        self.pairing_deadline = time.monotonic() + 120

    def exchange(self, token):
        if time.monotonic() > self.pairing_deadline or not self.pairing or not secrets.compare_digest(token, self.pairing):
            return None
        self.pairing = ""
        return self.bearer


def error_response(code, message, status=400):
    return JSONResponse({"error": {"code": code, "message": message, "details": {}}}, status_code=status)


class SecurityMiddleware:
    def __init__(self, app, auth, port):
        self.app, self.auth, self.port = app, auth, port

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        origin = f"http://127.0.0.1:{self.port}"
        valid_hosts = {f"127.0.0.1:{self.port}"}
        path = scope["path"]
        response = None
        if headers.get("host") not in valid_hosts:
            response = error_response("HOST_DENIED", "Use the numeric 127.0.0.1 loopback URL", 403)
        elif path.startswith("/api/") and path != "/api/health":
            if (headers.get("origin") is not None and headers.get("origin") != origin) or headers.get("origin", headers.get("x-blackbox-origin")) != origin:
                response = error_response("ORIGIN_DENIED", "Missing or invalid same-origin header", 403)
            elif path != "/api/pair" and not secrets.compare_digest(headers.get("authorization", ""), "Bearer " + self.auth.bearer):
                response = error_response("AUTH_REQUIRED", "Pair this tab with the short-lived link from BlackBox", 401)
        try:
            size = int(headers.get("content-length", "0"))
        except ValueError:
            size = 100_000_000
        if size > 34 * 1024 * 1024:
            response = error_response("BODY_TOO_LARGE", "Maximum request size is 34 MiB", 413)
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [(b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store"), (b"x-frame-options", b"DENY")]
            await send(message)
        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > 34 * 1024 * 1024:
                from starlette.exceptions import HTTPException
                raise HTTPException(413, "Maximum request size is 34 MiB")
            return message
        if response:
            return await response(scope, receive, secure_send)
        await self.app(scope, limited_receive, secure_send)
