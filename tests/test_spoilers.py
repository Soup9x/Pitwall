"""Spoiler mode: one rule for alerts and pages, and held data never reaches the browser."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, FollowedDriver, Outbox
from pitwall.notify import builders, outbox
from pitwall.notify.message import Message
from pitwall.services import digest, spoilers
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import load_settings, save_settings
from pitwall.web import auth
from tests.conftest import dt, load, races

PASSWORD = "correct horse battery"
QUALI_AT = dt("2025-10-25T23:30:00")  # when the qualifying results arrived
RACE_AT = dt("2025-10-26T22:00:00")  # when the race results arrived


@pytest.fixture
def mexico(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for race in races("qualifying_2025_3.json"):
        apply_results(db, race, "Q", now=QUALI_AT)
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=RACE_AT)
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    apply_standings(
        db,
        "constructor",
        load("constructor_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    db.add(FollowedDriver(driver_id="leclerc"))
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _get(client, path, at):
    """GET `path` as admin at time `at` (logging in then, so the session cookie is valid)."""
    with time_machine.travel(at, tick=False):
        client.cookies.clear()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
        r = client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "csrf_token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303
        return client.get(path)


def test_pages_and_alerts_share_the_auto_release_rule(db, client, mexico, sent):
    save_settings(db, {"spoiler_enabled": True, "spoiler_auto_release_hours": 12})
    outbox.enqueue(
        db,
        Message(type="results", title="Race results", text="P1 Norris", event_id=mexico.id),
        now=RACE_AT,
    )
    db.commit()

    almost = RACE_AT + timedelta(hours=11, minutes=59)
    page = _get(client, f"/events/{mexico.id}", almost).text
    assert "Hidden by spoiler mode" in page and "Piastri" not in page
    outbox.flush(db, now=almost)
    db.commit()
    assert sent.items == []

    released = RACE_AT + timedelta(hours=12)
    page = _get(client, f"/events/{mexico.id}", released).text
    assert "Hidden by spoiler mode" not in page and "Piastri" in page
    outbox.flush(db, now=released)
    assert len(sent.items) == 1


def test_auto_release_clock_restarts_with_each_sessions_results(db, mexico):
    save_settings(db, {"spoiler_enabled": True, "spoiler_auto_release_hours": 12})
    settings = load_settings(db)
    # The race results arrived after qualifying's 12 hours were up: the race is held again.
    assert spoilers.held(db, mexico.id, 1, settings, RACE_AT + timedelta(hours=1))
    assert not spoilers.held(db, mexico.id, 1, settings, RACE_AT + timedelta(hours=12))
    # Without auto-release only "Watched" releases it; spoiler mode off releases everything.
    save_settings(db, {"spoiler_auto_release_hours": 0})
    assert spoilers.held(db, mexico.id, 1, load_settings(db), RACE_AT + timedelta(days=30))
    spoilers.mark_watched(db, mexico.id, user_id=1)
    assert not spoilers.held(db, mexico.id, 1, load_settings(db), RACE_AT)
    spoilers.mark_watched(db, mexico.id, False, user_id=1)
    save_settings(db, {"spoiler_enabled": False})
    assert not spoilers.held(db, mexico.id, 1, load_settings(db), RACE_AT)


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/", "Piastri"),
        ("/standings?season=2025", "Piastri"),
        ("/head-to-head?season=2025", "Piastri"),
        ("/drivers/norris?season=2025", "Race by race"),
        ("/events/{event}", "Piastri"),
    ],
)
def test_held_data_is_not_in_the_page_source(db, client, mexico, path, marker):
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    url = path.format(event=mexico.id)
    later = RACE_AT + timedelta(hours=2)
    html = _get(client, url, later).text
    assert marker not in html  # not merely collapsed: absent from the HTML
    assert "Hidden by spoiler mode" in html and "Mexico City Grand Prix" in html
    revealed = _get(client, url + ("&" if "?" in url else "?") + "reveal=1", later).text
    assert marker in revealed
    with time_machine.travel(later, tick=False):
        spoilers.mark_watched(db, mexico.id, user_id=1)
        db.commit()
    assert marker in _get(client, url, later).text


def test_trivia_waits_for_the_held_event(db, client, mexico):
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    later = RACE_AT + timedelta(hours=2)
    other = db.scalars(select(Event).where(Event.slug == "2025-albert_park")).one()
    assert "Did you know?" not in _get(client, f"/events/{other.id}", later).text


def test_csv_exports_refuse_while_held(db, client, mexico):
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    later = RACE_AT + timedelta(hours=2)
    r = _get(client, "/export/standings.csv?season=2025", later)
    assert r.status_code == 409 and "Piastri" not in r.text
    assert _get(client, "/export/standings.csv?season=2025&reveal=1", later).status_code == 200
    assert _get(client, "/export/standings.csv?season=2024", later).status_code != 409


def test_preview_email_leaves_out_standings_form_and_trivia(db, mexico):
    save_settings(db, {"spoiler_enabled": True})
    other = db.scalars(select(Event).where(Event.slug == "2025-albert_park")).one()
    later = RACE_AT + timedelta(hours=2)
    msg = builders.preview(db, other, load_settings(db), later)
    for body in (msg.text, msg.html or ""):
        assert "Piastri" not in body and "Norris" not in body
        assert "until you mark Mexico City Grand Prix as watched" in body
    assert msg.data["title_math"] == []

    spoilers.mark_watched(db, mexico.id, user_id=1)
    msg = builders.preview(db, other, load_settings(db), later)
    assert "Piastri" in msg.text and "until you mark" not in msg.text


def test_digest_leaves_out_standings_while_held(db, mexico):
    save_settings(
        db,
        {"spoiler_enabled": True, "digest_enabled": True, "digest_types": ["schedule_change"]},
    )
    later = RACE_AT + timedelta(hours=2)
    outbox.enqueue(
        db, Message(type="schedule_change", title="Time change", text="FP1 moved"), now=later
    )
    outbox.flush(db, now=later)
    assert db.scalars(select(Outbox)).one().hold_reason == "digest"

    built = digest.build(db, load_settings(db), later, "http://pitwall")
    assert built is not None and "Piastri" not in built[0].text
    spoilers.mark_watched(db, mexico.id, user_id=1)
    built = digest.build(db, load_settings(db), later, "http://pitwall")
    assert built is not None and "Piastri" in built[0].text
