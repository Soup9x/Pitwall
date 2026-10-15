"""Phase 3: weather, circuit maps, weekly digest and CSV exports."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from pitwall import jobs
from pitwall.models import (
    Circuit,
    Event,
    FollowedDriver,
    NotificationLog,
    Outbox,
    SessionWeather,
)
from pitwall.notify import builders, outbox
from pitwall.notify.message import Message
from pitwall.services import circuit_maps, digest, exports, weather
from pitwall.settings_store import get_kv, load_settings, save_settings
from pitwall.sources.http import RateLimiter
from pitwall.sources.openf1 import OpenF1Client
from pitwall.sources.openmeteo import OpenMeteoClient
from pitwall.web import auth
from tests.world import World

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2025, 3, 10, 12, tzinfo=UTC)


@pytest.fixture
def world(db):
    w = World(db)
    c = w.circuit("testford", "UK", name="Testford Park")
    c.lat, c.lng = 51.0, -1.0
    w.circuit("filler", "Nowhere")
    for did in ("alice_ace", "bob_bolt"):
        w.driver(did, "British", date(1999, 1, 1))
    w.con("redline", "British", "Redline")
    r1 = w.event(2025, 1, "filler")  # race Sat 2025-03-08 14:00 UTC
    w.results(
        r1,
        "RACE",
        [
            ("alice_ace", "redline", 1, 2, 25, "Finished", 4),
            ("bob_bolt", "redline", 2, 1, 18, "Finished", 81),
        ],
    )
    w.event(2025, 2, "testford")  # quali Fri 03-14 14:00, race Sat 03-15 14:00
    w.standings(2025, 1, [("alice_ace", 25, 1), ("bob_bolt", 18, 0)], [("redline", 43, 1)])
    db.commit()
    return w


def meteo(calls=None, status=200):
    def handler(req):
        if calls is not None:
            calls.append(req)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=json.loads((FIX / "openmeteo/forecast.json").read_text()))

    return OpenMeteoClient(
        limiter=RateLimiter(),
        sleep=lambda _s: None,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------- weather


def test_summarize_window():
    hourly = json.loads((FIX / "openmeteo/forecast.json").read_text())["hourly"]
    fc = weather.summarize(
        hourly, datetime(2025, 3, 15, 14, tzinfo=UTC), datetime(2025, 3, 15, 16, tzinfo=UTC)
    )
    assert fc.rain_chance == 55 and fc.precip_mm == 2.4 and fc.weather_code == 63
    assert fc.wind_kmh is not None and fc.temp_c is not None
    assert (
        weather.summarize(
            hourly, datetime(2025, 4, 1, tzinfo=UTC), datetime(2025, 4, 1, 2, tzinfo=UTC)
        )
        is None
    )


def test_sync_flags_rain_risk_on_quali_or_race_only(db, world):
    calls = []
    assert weather.sync(meteo(calls), NOW) == 2  # quali + race of round 2 (round 1 is past)
    assert len(calls) == 1 and "latitude=51.0" in str(calls[0].url)
    ev = db.scalars(select(Event).where(Event.round == 2)).one()
    views = weather.for_event(db, ev)
    by_type = {s.type: views[s.id] for s in ev.sessions}
    assert by_type["Q"].rain_chance == 10 and not by_type["Q"].risk
    assert by_type["RACE"].risk and by_type["RACE"].summary == "Rain"
    assert weather.rain_warnings(ev, views) == ["Rain risk 55% for Race"]
    assert "°C" in by_type["RACE"].temp and "°F" in by_type["RACE"].temp


def test_weather_outage_keeps_previous_forecast(db, world):
    from pitwall.models import ApiCache

    weather.sync(meteo(), NOW)
    db.query(ApiCache).delete()  # nothing cached to fall back on
    db.commit()
    assert weather.sync(meteo(status=503), NOW) == 0
    db.expire_all()
    assert db.query(SessionWeather).count() == 2  # earlier forecasts are kept


def test_preview_shows_weather_and_rain_risk(db, world):
    weather.sync(meteo(), NOW)
    ev = db.scalars(select(Event).where(Event.round == 2)).one()
    msg = builders.preview(db, ev, load_settings(db), NOW)
    assert "! Rain risk 55% for Race" in msg.text
    assert "Rain risk 55% for Race" in msg.html and "<th>Weather</th>" in msg.html
    assert msg.data["rain_risk"] == ["Rain risk 55% for Race"]


# ---------------------------------------------------------------------------- maps


def openf1(routes):
    def handler(req):
        path = req.url.path.split("/v1/")[-1]
        body = routes.get(path)
        if body is None:
            return httpx.Response(404, json={"detail": "No results found."})
        return httpx.Response(200, json=json.loads((FIX / "openf1" / body).read_text()))

    return OpenF1Client(
        limiter=RateLimiter(),
        sleep=lambda _s: None,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )


def test_lap_points_and_render(tmp_path):
    laps = json.loads((FIX / "openf1/laps_map_9701_4.json").read_text())
    loc = json.loads((FIX / "openf1/location_9701_4.json").read_text())
    pts = circuit_maps.lap_points(laps, loc)
    assert 290 <= len(pts) <= 302  # one 80 s lap of the 160 s sample
    out = tmp_path / "m.png"
    circuit_maps.render(pts, out)
    img = Image.open(out)
    assert img.size == circuit_maps.SIZE and img.mode == "RGBA"
    assert img.getbbox() is not None  # something was drawn


def test_generate_missing_draws_once_per_week(db, world):
    routes = {
        "sessions": "sessions_2025_race.json",
        "laps": "laps_map_9701_4.json",
        "location": "location_9701_4.json",
    }
    # The filler circuit hosted round 1 (in OpenF1's list) and is on the calendar.
    with time_machine.travel(NOW, tick=False):
        assert circuit_maps.generate_missing(openf1(routes), NOW) == 1
    path = circuit_maps.maps_dir() / "filler.png"
    assert path.is_file()
    db.expire_all()
    assert circuit_maps.map_url(db.get(Circuit, "filler")).endswith("/circuit-maps/filler")
    assert "filler" in get_kv(db, circuit_maps.ATTEMPTS_KEY)
    # Testford has no past race, so nothing to draw; nothing is retried within a week.
    assert circuit_maps.generate_missing(openf1(routes), NOW) == 0


def test_map_url_precedence_and_safety(db, world, tmp_path):
    c = db.get(Circuit, "testford")
    assert circuit_maps.map_url(c) is None
    c.map_image = "https://example.com/testford.png"
    assert circuit_maps.map_url(c) == "https://example.com/testford.png"
    c.map_image = "../outside.png"
    (tmp_path.parent / "outside.png").write_bytes(b"x")
    assert circuit_maps.local_file(c) is None  # never serve files outside the data dir
    c.map_image = None
    (circuit_maps.maps_dir()).mkdir(parents=True, exist_ok=True)
    (circuit_maps.maps_dir() / "testford.svg").write_text("<svg/>")
    assert circuit_maps.local_file(c).name == "testford.svg"


# ---------------------------------------------------------------------------- digest


def _msg(type_, title, event_id=None):
    return Message(
        type=type_, title=title, text=f"{title} body\nhttps://pitwall.example/x", event_id=event_id
    )


def test_digest_holds_selected_types_and_sends_summary(db, world, sent):
    save_settings(
        db,
        {
            "digest_enabled": True,
            "digest_types": ["results", "standings"],
            "routing": {
                "results": ["webhook"],
                "standings": ["webhook"],
                "reminder": ["webhook"],
                "digest": ["email"],
            },
        },
    )
    outbox.enqueue(db, _msg("results", "🏁 Race results — Filler GP"), now=NOW)
    outbox.enqueue(db, _msg("standings", "🏆 Standings"), now=NOW)
    outbox.enqueue(db, _msg("reminder", "Race in 1 hour"), now=NOW)
    outbox.flush(db, now=NOW)
    assert sent.titles() == ["Race in 1 hour"]  # reminders are never held
    assert {r.hold_reason for r in digest.held(db)} == {"digest"}
    db.commit()

    with time_machine.travel(NOW, tick=False):
        jobs.digest_job(NOW)
    db.expire_all()
    titles = sent.titles("email")
    assert len(titles) == 1 and titles[0].startswith("🗞️ Your F1 week")
    body = sent.items[-1][1]
    assert "Race results — Filler GP" in body.text and "COMING UP THIS WEEK" in body.text
    assert "Qualifying, Testford GP" in body.text
    assert "https://pitwall.example/x" not in body.text  # per-item links dropped
    statuses = {r.title: r.status for r in db.scalars(select(Outbox))}
    assert statuses["🏆 Standings"] == "digested"
    assert db.scalars(select(NotificationLog).where(NotificationLog.status == "digested")).all()
    # Same day again: nothing new to send.
    with time_machine.travel(NOW, tick=False):
        jobs.digest_job(NOW)
    assert len(sent.titles("email")) == 1


def test_disabling_digest_releases_held_alerts(db, world, sent):
    save_settings(db, {"digest_enabled": True})
    outbox.enqueue(db, _msg("results", "R"), now=NOW)
    outbox.flush(db, now=NOW)
    assert sent.items == []
    save_settings(db, {"digest_enabled": False})
    outbox.flush(db, now=NOW)
    assert sent.titles() == ["R"]


def test_routing_saved_before_digest_existed_gets_default(db):
    from pitwall.models import Setting

    db.add(Setting(key="routing", value=json.dumps({"results": ["email"]})))
    db.flush()
    s = load_settings(db)
    assert s.routing["digest"] == ["email"] and s.routing["results"] == ["email"]


# ---------------------------------------------------------------------------- exports


def test_csv_exports(db, world):
    rows = list(csv.reader(io.StringIO(exports.standings_csv(db, 2025, "driver"))))
    assert rows[0][:6] == ["season", "round", "position", "id", "name", "team"]
    assert rows[1][:5] == ["2025", "1", "1", "alice_ace", "Alice Ace"]
    assert rows[2][8] == "7.0"  # gap to leader
    assert exports.standings_csv(db, 1999, "driver") is None
    db.add(FollowedDriver(driver_id="bob_bolt"))
    db.flush()
    rows = list(csv.reader(io.StringIO(exports.drivers_csv(db, 2025))))
    assert rows[1][3:8] == ["bob_bolt", "Bob Bolt", "Redline", "1", "P2"]
    assert rows[1][8] == "-1"


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def login(c):
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"password": "correct horse battery", "csrf_token": token})


def test_routes(client, db, world):
    assert client.get("/export/standings.csv", follow_redirects=False).status_code == 303
    assert client.get("/circuit-maps/testford").status_code == 404
    circuit_maps.maps_dir().mkdir(parents=True, exist_ok=True)
    circuit_maps.render(
        [(0, 0), (100, 0), (100, 50), (0, 50)] * 20, circuit_maps.maps_dir() / "testford.png"
    )
    r = client.get("/circuit-maps/testford")  # public: email clients need it without login
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/circuit-maps/..%2Fsecret").status_code == 404

    login(client)
    r = client.get("/export/standings.csv?season=2025&kind=constructor")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "redline,Redline,43.0" in r.text
    assert client.get("/export/standings.csv?kind=bogus").status_code == 404
    r = client.get("/export/drivers.csv?season=2025&driver=alice_ace")
    assert r.status_code == 200 and "Alice Ace" in r.text
    ev = db.scalars(select(Event).where(Event.round == 2)).one()
    page = client.get(f"/events/{ev.id}").text
    assert 'class="track-map"' in page and "/circuit-maps/testford" in page
    s = client.get("/settings").text
    assert "Weekly digest" in s and 'name="digest_day"' in s
