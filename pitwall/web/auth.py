"""Single-password login with a signed session cookie, CSRF tokens and login rate limiting."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from pitwall.db import session_scope
from pitwall.models import User

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
    """The visitor's IP, for rate limiting. Behind a proxy or Cloudflare Tunnel, uvicorn has
    already replaced the proxy's address with the real visitor's (from X-Forwarded-For) - but
    only for requests from addresses listed in FORWARDED_ALLOW_IPS, so nobody else can fake
    it by sending that header themselves."""
    return request.client.host if request.client else "unknown"


def login_session(request: Request, user: User) -> None:
    request.session.clear()  # new session on login (no fixation)
    request.session["uid"] = user.id
    request.session["ver"] = user.session_version


def session_user(request: Request) -> User | None:
    """The logged-in user, or None. Sessions die when the password changes or the account is
    disabled or deleted."""
    uid, ver = request.session.get("uid"), request.session.get("ver")
    if not uid:
        return None
    with session_scope() as db:
        user = db.get(User, uid)
        if user is None or not user.active or user.session_version != ver:
            request.session.clear()
            return None
        db.expunge(user)
    return user


def is_logged_in(request: Request) -> bool:
    return session_user(request) is not None


def require_user(request: Request) -> User:
    user = session_user(request)
    if user is None:
        path = request.url.path
        if request.url.query:
            path += f"?{request.url.query}"
        raise NotAuthenticated(path)
    request.state.user = user
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


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
