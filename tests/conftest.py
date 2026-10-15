from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session as DB

FIXTURES = Path(__file__).parent / "fixtures" / "jolpica"

TEST_ENV = {
    "TZ": "America/New_York",
    "PITWALL_ADMIN_PASSWORD": "correct horse battery",
    "PITWALL_ICS_TOKEN": "ics-test-token-123",
    "PITWALL_SECRET_KEY": "test-secret-key",
    "PITWALL_BASE_URL": "https://pitwall.example",
    "APPRISE_URLS_EMAIL": "mailtos://user:pass@smtp.example.com",
    "APPRISE_URLS_WEBHOOK": "jsons://hooks.example.com/pitwall",
    "PITWALL_SCHEDULER_ENABLED": "false",
}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def races(name: str) -> list[dict[str, Any]]:
    return load(name)["MRData"]["RaceTable"]["Races"]


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    from pitwall.config import get_env

    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PITWALL_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # keep any stray .env out of tests
    get_env.cache_clear()
    yield
    get_env.cache_clear()


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    from pitwall.db import make_engine, set_engine
    from pitwall.migrate import upgrade

    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Any) -> Iterator[DB]:
    from pitwall.db import session_scope

    with session_scope() as s:
        yield s


class FixtureRoutes:
    """Maps Jolpica URL paths (e.g. '2025.json') to fixture files or callables."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.calls: list[str] = []

    def set(self, path: str, value: Any) -> None:
        self.routes[path] = value

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/ergast/f1/", 1)[-1]
        self.calls.append(path)
        target = self.routes.get(path)
        if target is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(target):
            return target(request)
        if isinstance(target, int):
            return httpx.Response(target)
        body = load(target) if isinstance(target, str) else target
        return httpx.Response(200, json=body)


@pytest.fixture
def api(engine: Any) -> Iterator[tuple[FixtureRoutes, Callable[[], Any]]]:
    from pitwall import jobs
    from pitwall.sources.http import RateLimiter
    from pitwall.sources.jolpica import JolpicaClient

    routes = FixtureRoutes()

    def factory() -> JolpicaClient:
        return JolpicaClient(
            "https://api.jolpi.ca/ergast/f1",
            limiter=RateLimiter(),
            sleep=lambda _s: None,
            max_retries=2,
            transport=httpx.MockTransport(routes.handler),
        )

    jobs.set_client_factory(factory)
    yield routes, factory
    from pitwall.sources.jolpica import make_client

    jobs.set_client_factory(make_client)


class Sent:
    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []
        self.fail: set[str] = set()

    def __call__(self, channel: str, msg: Any) -> tuple[bool, str]:
        if channel in self.fail:
            return False, "boom"
        self.items.append((channel, msg))
        return True, "ok"

    def titles(self, channel: str | None = None) -> list[str]:
        return [m.title for c, m in self.items if channel is None or c == channel]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    """Capture deliveries instead of calling Apprise."""
    from pitwall.notify import outbox

    s = Sent()
    monkeypatch.setattr(outbox, "notify", s)
    return s


@pytest.fixture(autouse=True)
def offline_clients():
    """No test may reach the network: by default every job gets clients whose transport
    answers 503 immediately (tests that need data install their own fixtures)."""
    from pitwall import jobs
    from pitwall.sources.http import RateLimiter
    from pitwall.sources.jolpica import JolpicaClient
    from pitwall.sources.jolpica import make_client as jolpica_default
    from pitwall.sources.openf1 import OpenF1Client
    from pitwall.sources.openf1 import make_client as openf1_default
    from pitwall.sources.openmeteo import OpenMeteoClient
    from pitwall.sources.openmeteo import make_client as meteo_default

    def offline(cls):
        return lambda: cls(
            limiter=RateLimiter(),
            sleep=lambda _s: None,
            max_retries=0,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        )

    jobs.set_client_factory(
        lambda: JolpicaClient(
            "https://api.jolpi.ca/ergast/f1",
            limiter=RateLimiter(),
            max_retries=0,
            sleep=lambda _s: None,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
        )
    )
    jobs.set_openf1_factory(offline(OpenF1Client))
    jobs.set_weather_factory(offline(OpenMeteoClient))
    yield
    jobs.set_client_factory(jolpica_default)
    jobs.set_openf1_factory(openf1_default)
    jobs.set_weather_factory(meteo_default)
