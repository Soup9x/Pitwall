"""Polite HTTP client: descriptive User-Agent, rate limiting, retries with exponential backoff,
and a DB-backed response cache that keeps serving stale data when an API is down."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx

from pitwall import USER_AGENT
from pitwall.db import session_scope, utcnow
from pitwall.models import ApiCache, SourceStatus

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class SourceUnavailable(Exception):
    """Raised when an API failed and there is no cached response to fall back on."""


class RateLimiter:
    """Minimum spacing between requests plus a rolling hourly cap."""

    def __init__(
        self,
        min_interval: float = 0.0,
        per_hour: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.per_hour = per_hour
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0
        self._window: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._window and now - self._window[0] > 3600:
                    self._window.popleft()
                wait = max(0.0, self._last + self.min_interval - now)
                if self.per_hour and len(self._window) >= self.per_hour:
                    wait = max(wait, 3600 - (now - self._window[0]) + 0.1)
                if wait <= 0:
                    self._last = now
                    self._window.append(now)
                    return
            log.debug("rate limiter waiting", extra={"wait_s": round(wait, 2)})
            self.sleep(wait)


class CachedClient:
    def __init__(
        self,
        source: str,
        base_url: str,
        *,
        limiter: RateLimiter | None = None,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def url_for(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            url = f"{url}?{query}"
        return url

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_age: timedelta | None = timedelta(0),
        empty_on_404: bool = False,
        store: bool = True,
    ) -> Any:
        """Fetch JSON. If a cached copy is younger than `max_age` it is used without a request
        (`max_age=None` means cached copies never expire). On failure, any cached copy is
        returned and a warning logged; with no cache, SourceUnavailable is raised.
        `empty_on_404` treats a 404 as an empty list (for APIs that answer "no data" that way)."""
        url = self.url_for(path, params)
        with session_scope() as db:
            cached = db.get(ApiCache, url)
            cached_body = cached.body if cached else None
            fresh = cached is not None and (
                max_age is None or utcnow() - cached.fetched_at <= max_age
            )
        if fresh and cached_body is not None:
            return json.loads(cached_body)

        try:
            data = self._fetch(url)
        except httpx.HTTPStatusError as exc:
            if not (empty_on_404 and exc.response.status_code == 404):
                return self._failed(url, exc, cached_body)
            data = []
        except Exception as exc:
            return self._failed(url, exc, cached_body)

        with session_scope() as db:
            if not store:  # large one-off responses aren't worth keeping in the cache
                self._record_success(db)
                return data
            row = db.get(ApiCache, url)
            body = json.dumps(data)
            if row is None:
                db.add(ApiCache(url=url, body=body, fetched_at=utcnow()))
            else:
                row.body = body
                row.fetched_at = utcnow()
            self._record_success(db)
        return data

    def _failed(self, url: str, exc: Exception, cached_body: str | None) -> Any:
        self._record_error(str(exc))
        if cached_body is not None:
            log.warning(
                "API request failed; serving cached data",
                extra={"source": self.source, "url": url, "error": str(exc)},
            )
            return json.loads(cached_body)
        log.warning(
            "API request failed with no cached data",
            extra={"source": self.source, "url": url, "error": str(exc)},
        )
        raise SourceUnavailable(f"{self.source}: {exc}") from exc

    def _fetch(self, url: str) -> Any:
        attempt = 0
        while True:
            self.limiter.acquire()
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff_base**attempt
                log.info(
                    "transport error, retrying",
                    extra={"source": self.source, "error": str(exc), "delay_s": delay},
                )
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES or attempt >= self.max_retries:
                    resp.raise_for_status()
                delay = self.backoff_base**attempt
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 300.0))
                log.info(
                    "retryable HTTP status",
                    extra={"source": self.source, "status": resp.status_code, "delay_s": delay},
                )
            self.sleep(delay)
            attempt += 1

    def _record_success(self, db: Any) -> None:
        row = db.get(SourceStatus, self.source)
        if row is None:
            row = SourceStatus(source=self.source)
            db.add(row)
        row.last_success_at = utcnow()

    def _record_error(self, err: str) -> None:
        try:
            with session_scope() as db:
                row = db.get(SourceStatus, self.source)
                if row is None:
                    row = SourceStatus(source=self.source)
                    db.add(row)
                row.last_error_at = utcnow()
                row.last_error = err[:500]
        except Exception:  # pragma: no cover - never let bookkeeping crash a sync
            log.exception("failed to record source error")
