"""Retry/backoff, caching and graceful degradation of the API client."""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from pitwall import USER_AGENT
from pitwall.models import SourceStatus
from pitwall.sources.http import RateLimiter, SourceUnavailable
from pitwall.sources.jolpica import JolpicaClient
from tests.conftest import load


def make(handler, sleeps=None):
    return JolpicaClient(
        "https://api.jolpi.ca/ergast/f1",
        limiter=RateLimiter(),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        max_retries=3,
        transport=httpx.MockTransport(handler),
    )


def test_retries_with_exponential_backoff_then_succeeds(engine):
    calls = []
    sleeps: list[float] = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429 if len(calls) == 1 else 502)
        return httpx.Response(200, json=load("schedule_2025.json"))

    races = make(handler, sleeps).schedule(2025)
    assert len(races) == 3 and len(calls) == 3
    assert sleeps == [1.0, 2.0]
    assert calls[0].headers["user-agent"] == USER_AGENT


def test_retry_after_header_respected(engine):
    calls = []
    sleeps: list[float] = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=load("schedule_2025.json"))

    make(handler, sleeps).schedule(2025)
    assert sleeps == [7.0]


def test_outage_serves_cached_copy(engine, db):
    ok = make(lambda r: httpx.Response(200, json=load("schedule_2025.json")))
    assert len(ok.schedule(2025)) == 3
    down = make(lambda r: httpx.Response(503))
    assert len(down.schedule(2025)) == 3  # stale cache, no crash
    status = db.scalars(select(SourceStatus)).one()
    assert status.last_success_at is not None and status.last_error_at is not None


def test_outage_without_cache_raises_source_unavailable(engine):
    with pytest.raises(SourceUnavailable):
        make(lambda r: httpx.Response(500)).schedule(2025)


def test_client_errors_are_not_retried(engine):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(404)

    with pytest.raises(SourceUnavailable):
        make(handler).schedule(2025)
    assert len(calls) == 1


def test_fresh_cache_avoids_request(engine):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=load("schedule_2025.json"))

    client = make(handler)
    client.schedule(2025, max_age=timedelta(hours=1))
    client.schedule(2025, max_age=timedelta(hours=1))
    client.schedule(2025, max_age=None)
    assert len(calls) == 1


def test_pagination_merges_rows_of_same_race(engine):
    page1 = load("results_2025_3.json")
    page2 = load("results_2025_3.json")
    rows = page1["MRData"]["RaceTable"]["Races"][0]["Results"]
    page1["MRData"]["RaceTable"]["Races"][0]["Results"] = rows[:4]
    page2["MRData"]["RaceTable"]["Races"][0]["Results"] = rows[4:]
    for p, off in ((page1, 0), (page2, 4)):
        p["MRData"].update({"limit": "4", "offset": str(off), "total": str(len(rows))})

    def handler(req):
        return httpx.Response(200, json=page1 if req.url.params["offset"] == "0" else page2)

    races = make(handler).results(2025, 3)
    assert len(races) == 1 and len(races[0]["Results"]) == len(rows)


def test_rate_limiter_spacing_and_hourly_cap():
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        time.sleep(min(s, 0.05))

    lim = RateLimiter(min_interval=0.05, sleep=sleep)
    lim.acquire()
    lim.acquire()  # must wait for the minimum spacing
    assert waits and 0 < waits[0] <= 0.05

    capped = RateLimiter(per_hour=2, sleep=lambda s: (_ for _ in ()).throw(TimeoutError(s)))
    capped.acquire()
    capped.acquire()
    with pytest.raises(TimeoutError) as exc:
        capped.acquire()  # third request in an hour must wait ~1h
    assert exc.value.args[0] > 3500
