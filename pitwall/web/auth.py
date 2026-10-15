"""Single-password login with a signed session cookie, CSRF tokens and login rate limiting."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from pitwall.config import get_env

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


class NotAuthenticated(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


class LoginRateLimiter:
    def __init__(self, max_failures: int = MAX_FAILURES, window: int = WINDOW_SECONDS) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


limiter = LoginRateLimiter()


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_password(candidate: str) -> bool:
    expected = get_env().admin_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_user(request: Request) -> None:
    if not is_logged_in(request):
        path = request.url.path
        if request.url.query:
            path += f"?{request.url.query}"
        raise NotAuthenticated(path)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def checked_form(request: Request) -> FormData:
    """Parse a POSTed form and verify its CSRF token."""
    form = await request.form()
    sent = str(form.get("csrf_token", ""))
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return form


def safe_next(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path
