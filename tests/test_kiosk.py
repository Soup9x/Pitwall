"""Kiosk: per-user secret link, read-only page without links, JSON refresh, and the owner's
spoiler state always applies."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.logging_setup import RedactingFilter
from pitwall.models import Event, FollowedDriver, User
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import save_settings
from pitwall.web import auth, kiosk
from tests.conftest import dt, load, races

TOKEN = "kiosk-token-for-tests-0123456789"
RACE_AT = dt("2025-10-26T22:00:00")  # results arrived
FRIDAY = dt("2025-10-24T12:00:00")  # race weekend, before any results


@pytest.fixture
def mexico(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    apply_standings(
        db,
        "constructor",
        load("constructor_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    db.add(FollowedDriver(driver_id="leclerc"))
    db.get(User, 1).kiosk_token = TOKEN
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


@pytest.mark.parametrize("path", ["/kiosk", "/kiosk/data"])
@pytest.mark.parametrize("token", ["", "short", "x" * 30, TOKEN + "x"])
def test_wrong_token_is_a_plain_404(client, mexico, path, token):
    r = client.get(path, params={"token": token})
    assert r.status_code == 404 and "kiosk" not in r.text.lower()


def test_inactive_user_or_turned_off_link_is_404(client, db, mexico):
    assert client.get("/kiosk/data", params={"token": TOKEN}).status_code == 200
    db.get(User, 1).active = False
    db.commit()
    assert client.get("/kiosk/data", params={"token": TOKEN}).status_code == 404


def test_page_is_read_only_and_self_contained(client, mexico):
    with time_machine.travel(FRIDAY, tick=False):
        r = client.get("/kiosk", params={"token": TOKEN, "layout": "portrait"})
    assert r.status_code == 200
    html = r.text
    assert "<a " not in html and "<form" not in html  # no links into the app
    assert 'class="kiosk portrait"' in html
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert tag == '<script type="application/json" id="kiosk-data">' or re.fullmatch(
            r'<script src="/static/kiosk\.js\?v=[0-9a-f]{12}">', tag
        )
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers  # no login, no session
    data = json.loads(re.search(r'id="kiosk-data">(.*?)</script>', html, re.S)[1])
    assert data["weekend"] is True and data["event"]["name"] == "Mexico City Grand Prix"
    assert [s["label"] for s in data["timeline"]][-1] == "Race"
    assert data["next"]["session"] and data["next"]["start"]
    assert [d["code"] for d in data["drivers"]][:2] == ["NOR", "PIA"]
    assert next(d for d in data["drivers"] if d["code"] == "LEC")["followed"] is True
    assert len(data["constructors"]) <= 5 and len(data["drivers"]) <= 10
    for asset in ("/static/kiosk.js", "/static/kiosk.css"):
        assert client.get(asset).status_code == 200


def test_data_endpoint(client, mexico):
    with time_machine.travel(dt("2025-10-13T12:00:00"), tick=False):
        data = client.get("/kiosk/data", params={"token": TOKEN}).json()
    assert data["weekend"] is False and data["tz"] == "America/New_York"
    assert data["generated_at"].startswith("2025-10-13T12:00")
    assert data["held"] is None and data["round"] == 3


def test_follows_the_owners_spoiler_state(client, db, mexico):
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=RACE_AT)
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    later = RACE_AT + timedelta(hours=1)
    with time_machine.travel(later, tick=False):
        raw = client.get("/kiosk/data", params={"token": TOKEN}).text
        page = client.get("/kiosk", params={"token": TOKEN, "reveal": "1"}).text
    data = json.loads(raw)
    assert data["held"] == "Mexico City Grand Prix"
    assert data["drivers"] == [] and data["constructors"] == [] and data["trivia"] == []
    assert "Piastri" not in raw and "Norris" not in raw
    assert "Piastri" not in page  # there is no "show anyway" on a kiosk
    from pitwall.services import spoilers

    spoilers.mark_watched(db, mexico.id, user_id=1)
    db.commit()
    with time_machine.travel(later, tick=False):
        data = client.get("/kiosk/data", params={"token": TOKEN}).json()
    assert data["held"] is None and data["drivers"]


def test_account_page_creates_regenerates_and_turns_off(client, db, mexico):
    db.get(User, 1).kiosk_token = None
    db.commit()
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
    client.post(
        "/login",
        data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
    )
    page = client.get("/account").text
    assert "Create a kiosk link" in page and "/kiosk?token=" not in page
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    client.post("/account/kiosk", data={"csrf_token": csrf})
    db.expire_all()
    first = db.get(User, 1).kiosk_token
    assert first and len(first) >= 32
    page = client.get("/account").text
    assert f"/kiosk?token={first}" in page and "contains a secret" in page
    assert f'href="/kiosk?token={first}" target="_blank"' in page  # "Open kiosk" button
    client.post("/account/kiosk", data={"csrf_token": csrf})
    db.expire_all()
    second = db.get(User, 1).kiosk_token
    assert second != first
    assert client.get("/kiosk/data", params={"token": first}).status_code == 404
    client.post("/account/kiosk", data={"csrf_token": csrf, "action": "off"})
    db.expire_all()
    assert db.get(User, 1).kiosk_token is None
    assert client.get("/kiosk/data", params={"token": second}).status_code == 404


def test_tokens_never_reach_the_logs():
    f = RedactingFilter([])
    rec = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        '%s - "GET /kiosk/data?token=%s HTTP/1.1" 200',
        ("10.0.0.2:5000", TOKEN),
        None,
    )
    f.filter(rec)
    assert TOKEN not in rec.getMessage() and "token=***" in rec.getMessage()


def test_payload_without_any_events(db):
    db.get(User, 1).kiosk_token = TOKEN
    db.commit()
    data = kiosk.payload(db, kiosk.owner_for(db, TOKEN), dt("2025-01-05T12:00:00"))
    assert data["next"] is None and data["event"] is None and data["drivers"] == []
