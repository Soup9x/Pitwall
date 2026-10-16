"""Followed-driver tracker, OpenF1 detail, title math wiring and Phase 2 pages."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import DriverSessionDetail, Event, FollowedDriver
from pitwall.notify import builders
from pitwall.services import driver_stats, openf1_sync, title_math
from pitwall.settings_store import load_settings
from pitwall.sources.http import RateLimiter
from pitwall.sources.openf1 import OpenF1Client, fmt_lap, match_session, summarize
from pitwall.web import auth
from tests.world import World

OF1 = Path(__file__).parent / "fixtures" / "openf1"


@pytest.fixture
def season(db):
    """A fictional 2025 season: three rounds raced (one sprint weekend), two to go."""
    w = World(db)
    w.circuit("filler", "Nowhere")
    w.circuit("testford", "UK", name="Testford Park")
    for did, nat in (("alice_ace", "British"), ("bob_bolt", "Dutch"), ("cy_crash", "French")):
        w.driver(did, nat, date(1999, 1, 1))
    w.con("redline", "British", "Redline")
    w.con("azure", "Italian", "Azure")
    w.season(2024, 2025)
    r1 = w.event(2025, 1, "filler")
    w.results(
        r1,
        "Q",
        [
            ("alice_ace", "redline", 2, None, 0, "Finished"),
            ("bob_bolt", "redline", 1, None, 0, "Finished"),
        ],
    )
    w.results(
        r1,
        "RACE",
        [
            ("alice_ace", "redline", 1, 2, 25, "Finished", 4),
            ("bob_bolt", "redline", 3, 1, 15, "Finished", 81),
            ("cy_crash", "azure", 2, 3, 18, "Finished", 10),
        ],
    )
    r2 = w.event(2025, 2, "testford", sprint=True)
    w.results(
        r2,
        "Q",
        [
            ("alice_ace", "redline", 1, None, 0, "Finished"),
            ("bob_bolt", "redline", 2, None, 0, "Finished"),
        ],
    )
    w.results(
        r2,
        "SPRINT",
        [
            ("alice_ace", "redline", 1, 1, 8, "Finished"),
            ("bob_bolt", "redline", 4, 2, 5, "Finished"),
        ],
    )
    w.results(
        r2,
        "RACE",
        [
            ("alice_ace", "redline", 5, 1, 10, "Finished"),
            ("bob_bolt", "redline", 1, 2, 25, "Finished"),
            ("cy_crash", "azure", 9, 3, 2, "Finished"),
        ],
    )
    r3 = w.event(2025, 3, "filler")
    w.results(
        r3,
        "Q",
        [
            ("alice_ace", "redline", 3, None, 0, "Finished"),
            ("bob_bolt", "redline", 1, None, 0, "Finished"),
        ],
    )
    w.results(
        r3,
        "RACE",
        [
            ("alice_ace", "redline", 15, 3, 0, "Engine", 4),
            ("bob_bolt", "redline", 16, 1, 0, "Collision"),
            ("cy_crash", "azure", 1, 5, 25, "Finished"),
        ],
    )
    w.event(2025, 4, "testford")
    w.event(2025, 5, "filler")
    w.standings(
        2025,
        3,
        [("alice_ace", 43, 1), ("bob_bolt", 45, 1), ("cy_crash", 45, 1)],
        [("redline", 88, 2), ("azure", 45, 1)],
    )
    db.flush()
    return w


def test_driver_season_tracker(db, season):
    data = driver_stats.season(db, "alice_ace", 2025)
    assert [r.finish for r in data.rows] == ["P1", "P5", "R"]
    assert [r.points for r in data.rows] == [25, 18, 0]  # round 2 includes the sprint win
    assert [r.cumulative for r in data.rows] == [25, 43, 43]
    assert data.rows[1].sprint == "P1"
    assert data.avg_grid == 2.0 and data.avg_finish == 3.0
    assert data.positions_gained == 1 + (-4)
    assert data.dnfs == 1 and data.rows[2].dnf
    svg = driver_stats.trend_svg([("Alice Ace", [25, 43, 43])], ["Filler", "Testford", "Filler"])
    assert svg.startswith("<svg") and "<polyline" in svg and "43 pts after Testford" in svg


def test_openf1_helpers():
    sessions = json.loads((OF1 / "sessions_2025_race.json").read_text())
    assert match_session(sessions, datetime(2025, 3, 8, 14, 5, tzinfo=UTC)) == 9701
    assert match_session(sessions, datetime(2025, 4, 1, tzinfo=UTC)) is None
    best, pits, stints = summarize(
        json.loads((OF1 / "laps_9701_4.json").read_text()),
        json.loads((OF1 / "stints_9701_4.json").read_text()),
        json.loads((OF1 / "pit_9701_4.json").read_text()),
    )
    assert best == 91.907 and pits == 1
    assert [(s["compound"], s["laps"]) for s in stints] == [("MEDIUM", 3), ("HARD", 54)]
    assert fmt_lap(91.907) == "1:31.907" and fmt_lap(None) == "—"


def _openf1(routes):
    def handler(req):
        path = req.url.path.split("/v1/")[-1]
        params = dict(req.url.params)
        key = (path, params.get("session_key"), params.get("driver_number"))
        routes["calls"].append(key)
        body = routes.get(key)
        if body is None:
            return httpx.Response(404, json={"detail": "No results found."})
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, json=json.loads((OF1 / body).read_text()))

    return OpenF1Client(
        limiter=RateLimiter(),
        sleep=lambda _s: None,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )


def test_openf1_sync_for_followed_drivers(db, season):
    db.add(FollowedDriver(driver_id="alice_ace"))
    db.commit()
    routes = {
        "calls": [],
        ("sessions", None, None): "sessions_2025_race.json",
        ("laps", "9701", "4"): "laps_9701_4.json",
        ("stints", "9701", "4"): "stints_9701_4.json",
        ("pit", "9701", "4"): "pit_9701_4.json",
    }
    now = datetime(2025, 4, 1, tzinfo=UTC)
    # Only results with a car number can be looked up (rounds 1 and 3 here). Round 3 isn't in
    # OpenF1's session list, so it's stored empty and never re-requested.
    stored = openf1_sync.sync(_openf1(routes), now)
    db.expire_all()
    details = {d.session_id: d for d in db.scalars(select(DriverSessionDetail))}
    assert stored == len(details) == 2
    r1 = next(d for d in details.values() if d.openf1_session_key == 9701)
    assert r1.best_lap_s == 91.907 and r1.pit_count == 1
    assert json.loads(r1.stints_json)[0]["compound"] == "MEDIUM"
    calls = len(routes["calls"])
    assert openf1_sync.sync(_openf1(routes), now) == 0 and len(routes["calls"]) == calls

    data = driver_stats.season(db, "alice_ace", 2025)
    assert data.has_openf1 and data.rows[0].best_lap == "1:31.907"
    assert data.rows[0].pit_count == 1 and data.rows[2].best_lap is None


def test_openf1_outage_stops_and_retries_later(db, season):
    db.add(FollowedDriver(driver_id="alice_ace"))
    db.commit()
    routes = {"calls": [], ("sessions", None, None): 503}
    assert openf1_sync.sync(_openf1(routes), datetime(2025, 4, 1, tzinfo=UTC)) == 0
    assert db.scalars(select(DriverSessionDetail)).first() is None
    assert len(openf1_sync.pending_targets(datetime(2025, 4, 1, tzinfo=UTC))) == 2


def test_title_math_from_db(db, season):
    tm = title_math.for_season(db, 2025, "driver")
    # Two races left (no sprints) = 50 points: everyone within 50 of the leader is alive.
    assert tm.remaining_races == 2 and tm.max_remaining == 50
    assert tm.leader.name == "Bob Bolt" and len(tm.alive) == 3
    assert tm.next_event == "Testford Grand Prix" and not tm.can_clinch_next


def test_preview_includes_title_math_and_facts(db, season):
    target = db.scalars(select(Event).where(Event.round == 4)).one()
    msg = builders.preview(db, target, load_settings(db), datetime(2025, 3, 24, tzinfo=UTC))
    assert "DRIVERS' TITLE" in msg.text and "3 still in contention" in msg.text
    assert "DID YOU KNOW?" in msg.text and msg.data["facts"]
    assert "Did you know?" in msg.html and "title" in msg.html


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        c.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        yield c


def test_phase2_pages(client, db, season):
    db.commit()
    assert client.get("/head-to-head?season=2025").status_code == 404  # page removed
    d = client.get("/drivers/alice_ace?season=2025")
    assert d.status_code == 200 and "<polyline" in d.text and "Engine" in d.text
    assert client.get("/drivers/nobody").status_code == 404
    st = client.get("/standings?season=2025").text
    assert "title fight" in st and "/drivers/alice_ace" in st
    ev = db.scalars(select(Event).where(Event.round == 4)).one()
    assert "Did you know?" in client.get(f"/events/{ev.id}").text


def test_title_math_counts_unraced_sessions_of_the_standings_round(db):
    """Standings published after a Saturday sprint already say "round 2"; Sunday's race must
    still count as remaining, or a clinch could be announced too early."""
    w = World(db)
    w.circuit("filler", "Nowhere")
    w.driver("alice_ace")
    w.driver("bob_bolt")
    w.event(2025, 1, "filler")
    w.event(2025, 2, "filler", sprint=True)  # sprint Sat 2025-03-14 11:00, race Sun 14:00
    w.standings(2025, 2, [("alice_ace", 60, 2), ("bob_bolt", 40, 0)])
    mid = title_math.for_season(db, 2025, "driver", now=datetime(2025, 3, 14, 20, tzinfo=UTC))
    assert (mid.remaining_races, mid.remaining_sprints) == (1, 0)
    assert mid.next_event == "Filler Grand Prix" and not mid.next_is_sprint
    assert not mid.champion and mid.can_clinch_next
    after = title_math.for_season(db, 2025, "driver", now=datetime(2025, 3, 15, 20, tzinfo=UTC))
    assert after.remaining_races == 0 and after.champion.name == "Alice Ace"


def test_title_math_silent_without_calendar(db):
    w = World(db)
    w.driver("alice_ace")
    w.standings(2025, 5, [("alice_ace", 100, 4)])
    assert title_math.for_season(db, 2025, "driver") is None
