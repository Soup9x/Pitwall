"""Web UI, auth/CSRF, ICS token protection and /health."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, FollowedDriver
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import load_settings
from pitwall.web import auth
from tests.conftest import dt, load, races

PASSWORD = "correct horse battery"


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def csrf(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    r = client.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "csrf_token": csrf(client), "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client


@pytest.fixture
def seeded(db):
    apply_schedule(
        db,
        2025,
        races("schedule_2025.json"),
        now=dt("2025-03-01T00:00:00"),
        curated={"rodriguez": {"timezone": "America/Mexico_City", "turns": 17}},
    )
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    apply_standings(
        db,
        "constructor",
        load("constructor_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE")
    db.add(FollowedDriver(driver_id="leclerc"))
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def test_pages_require_login(client):
    for path in ("/", "/events", "/standings", "/settings", "/history"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_wrong_password_and_rate_limit(client):
    token = csrf(client)
    for _ in range(auth.MAX_FAILURES):
        r = client.post(
            "/login", data={"username": "admin", "password": "nope", "csrf_token": token}
        )
        assert r.status_code == 401
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 429  # even the right password is refused while limited


def test_login_requires_csrf(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 403


def test_open_redirect_blocked(client):
    r = client.post(
        "/login",
        data={
            "username": "admin",
            "password": PASSWORD,
            "csrf_token": csrf(client),
            "next": "//evil.example",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_health_is_public_and_secret_free(client, seeded):
    # From the internet: status only.
    r = client.get("/health", headers={"cf-connecting-ip": "203.0.113.9"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    # Logged in: details (but still never secrets).
    login(client)
    r = client.get("/health")
    body = r.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["scheduler"]["running"] is False
    text = r.text
    for secret in (
        PASSWORD,
        "ics-test-token-123",
        "test-secret-key",
        "smtp.example.com",
        "hooks.example.com",
    ):
        assert secret not in text


def test_ics_requires_token(client, seeded):
    assert client.get("/calendar.ics").status_code == 404
    assert client.get("/calendar.ics?token=wrong").status_code == 404
    r = client.get("/calendar.ics?token=ics-test-token-123&sessions=race")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.text.count("BEGIN:VEVENT") == 3


def test_all_pages_render(client, seeded):
    login(client)
    r = client.get("/")
    assert r.status_code == 200 and "Standings" in r.text and "Lando Norris" in r.text
    assert client.get("/events?season=2025").status_code == 200
    page = client.get(f"/events/{seeded.id}").text
    assert "Mexico City Grand Prix" in page and "Watched" in page and "Retired" in page
    assert client.get(f"/events/{seeded.id}/preview").status_code == 200
    assert "▲1" in client.get("/standings").text
    s = client.get("/settings").text
    assert "Charles Leclerc" in s
    assert "ics-test-token-123" not in s  # the calendar link lives on the Account page
    acct = client.get("/account").text
    assert "/calendar.ics?token=ics-test-token-123" in acct  # the owner keeps their old link
    assert "user:pass" not in s and "mailtos://smtp.example.com/…" in s
    assert client.get("/history").status_code == 200
    assert client.get("/events/99999").status_code == 404


def test_settings_save_and_validation(client, seeded, db):
    login(client)
    token = csrf(client, "/settings")
    form = {
        "csrf_token": token,
        "timezone": "Europe/London",
        "offset_RACE": "2h, 15m",
        "offset_Q": "1d",
        "route_reminder_email": "on",
        "quiet_enabled": "on",
        "quiet_start": "22:00",
        "quiet_end": "06:30",
        "odd_start": "00:00",
        "odd_end": "06:00",
        "spoiler_enabled": "on",
        "spoiler_auto_release_hours": "24",
        "backfill_start_year": "2010",
    }
    r = client.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    s = load_settings(db)
    assert s.timezone == "Europe/London"
    assert s.reminder_offsets["RACE"] == [120, 15] and s.reminder_offsets["Q"] == [1440]
    assert s.reminder_offsets["FP1"] == []
    assert s.routing["reminder"] == ["email"] and s.routing["results"] == []
    assert s.quiet_enabled and s.quiet_end == "06:30" and s.spoiler_enabled
    assert s.backfill_start_year == 2010

    bad = dict(form, offset_RACE="soon")
    client.post("/settings", data=bad, follow_redirects=False)
    assert "Can&#39;t read reminder offset" in client.get("/settings").text
    db.expire_all()
    assert load_settings(db).reminder_offsets["RACE"] == [120, 15]  # unchanged


def test_watched_button_and_csrf(client, seeded, db):
    login(client)
    assert client.post(f"/events/{seeded.id}/watched", data={"watched": "1"}).status_code == 403
    token = csrf(client, f"/events/{seeded.id}")
    r = client.post(
        f"/events/{seeded.id}/watched",
        data={"watched": "1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    from pitwall.notify.outbox import is_watched

    db.expire_all()
    assert is_watched(db, seeded.id)


def test_follow_and_unfollow(client, seeded, db):
    login(client)
    token = csrf(client, "/settings")
    client.post(
        "/settings/followed", data={"csrf_token": token, "action": "add", "driver_id": "norris"}
    )
    client.post(
        "/settings/followed", data={"csrf_token": token, "action": "remove", "driver_id": "leclerc"}
    )
    db.expire_all()
    assert {f.driver_id for f in db.scalars(select(FollowedDriver))} == {"norris"}


def test_logout(client):
    login(client)
    token = csrf(client, "/")
    client.post("/logout", data={"csrf_token": token})
    assert client.get("/", follow_redirects=False).status_code == 303
