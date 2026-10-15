"""Security headers for running on the public internet (e.g. behind a Cloudflare Tunnel).

Cookies are marked Secure on HTTPS requests only (as seen through a trusted proxy's
X-Forwarded-Proto), so the same install works at https://pitwall.example.com through the tunnel
and at http://server:8080 on the LAN. No HSTS header is sent: it would make browsers refuse the
plain-HTTP LAN address for that hostname."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Pages use only same-origin scripts (no inline JavaScript); inline styles are used in
# templates. Images may come from https URLs (circuit maps in circuits.yaml).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.headers = [
            (b"content-security-policy", CSP.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k not in existing]
                path = scope.get("path", "")
                if not path.startswith(("/static/", "/circuit-maps/", "/calendar.ics")):
                    extra.append((b"cache-control", b"no-store"))  # pages hold personal data
                headers = list(message.get("headers", []))
                if scope.get("scheme") == "https":
                    headers = [_secure_cookie(k, v) for k, v in headers]
                message["headers"] = [*headers, *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _secure_cookie(name: bytes, value: bytes) -> tuple[bytes, bytes]:
    if name.lower() == b"set-cookie" and b"; secure" not in value.lower():
        value += b"; secure"
    return name, value
