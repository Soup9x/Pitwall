"""OpenF1 post-session data: race control timeline/stats and measured conditions.

The OpenF1 responses are hand-built in the documented format (tests/fixtures/openf1/README.md)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, RaceControlMessage, SessionConditions, SessionDataFetch
from pitwall.services import session_data
from pitwall.services.sync import apply_results, apply_schedule
from pitwall.settings_store import save_settings
from pitwall.sources.http import RateLimiter
from pitwall.sources.openf1 import OpenF1Client
from pitwall.web import auth
from tests.conftest import dt, races

OF1 = Path(__file__).parent / "fixtures" / "openf1"
NOW = datetime(2025, 10, 28, tzinfo=UTC)


def _client(routes, calls=None):
    def handler(req):
        path = req.url.path.split("/v1/")[-1]
        params = dict(req.url.params)
        key = (path, params.get("year") or params.get("session_key"))
        if calls is not None:
            calls.append(key)
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


ROUTES = {
    ("sessions", "2025"): "sessions_2025_all.json",
    ("sessions", "2024"): "sessions_2024_all.json",
    ("sessions", "2023"): "sessions_2023_all.json",
    ("race_control", "9405"): "race_control_9405.json",
    ("race_control", "9905"): "race_control_9905.json",
    ("race_control", "9904"): "race_control_9904.json",
    ("race_control", "9505"): "race_control_9505.json",
    ("weather", "9905"): "weather_9905.json",
    ("weather", "9505"): "weather_9505.json",
}


@pytest.fixture
def mexico(db):
    for year in (2023, 2024):
        apply_schedule(
            db, year, races(f"schedule_{year}.json"), now=dt(f"{year}-03-01T00:00:00"), curated={}
        )
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def _sync(db, calls=None):
    n = session_data.sync(_client(ROUTES, calls), NOW, limit=100)
    db.expire_all()
    return n


def test_sync_stores_race_control_and_conditions_once(db, mexico):
    calls = []
    assert _sync(db, calls) > 0
    race = next(s for s in mexico.sessions if s.type == "RACE")
    msgs = list(
        db.scalars(select(RaceControlMessage).where(RaceControlMessage.session_id == race.id))
    )
    assert len(msgs) == 14 and msgs[0].message == "PIT EXIT OPEN"
    assert db.get(SessionDataFetch, race.id).openf1_session_key == 9905

    cond = db.get(SessionConditions, race.id)
    # The 999 °C glitch is dropped, not averaged in.
    assert (cond.track_min, cond.track_max) == (41.2, 51.6)
    assert (cond.air_min, cond.air_max) == (21.0, 22.9)
    assert cond.rain is False and cond.wind_max_ms == 3.0 and cond.samples == 12
    assert all(p[1] < 60 for p in json.loads(cond.series_json))

    # Completed sessions aren't fetched again.
    before = len(calls)
    assert _sync(db, calls) == 0 and len(calls) == before


def test_sessions_without_data_wait_then_give_up(db, mexico):
    # FP1-FP3 have no race control/weather in the fixtures. Soon after the weekend they're
    # retried later; two days on, the empty answer is final.
    session_data.sync(_client(ROUTES), datetime(2025, 10, 25, 20, tzinfo=UTC), limit=100)
    db.expire_all()
    fp1 = next(s for s in mexico.sessions if s.type == "FP1")
    assert db.get(SessionDataFetch, fp1.id) is None
    _sync(db)
    assert db.get(SessionDataFetch, fp1.id).race_control_count == 0
    assert db.get(SessionConditions, fp1.id) is None


def test_outage_stores_nothing(db, mexico):
    broken = {k: 503 for k in ROUTES}
    assert session_data.sync(_client(broken), NOW, limit=100) == 0
    assert db.scalars(select(SessionDataFetch)).first() is None


def test_race_control_stats(db, mexico):
    _sync(db)
    race = next(s for s in mexico.sessions if s.type == "RACE")
    quali = next(s for s in mexico.sessions if s.type == "Q")
    rc = {r.session.type: r for r in session_data.race_control_for(db, mexico.sessions)}
    st = rc["RACE"].stats
    assert (st.safety_cars, st.sc_laps) == (1, 4)  # deployed lap 3, in on lap 6
    assert (st.vscs, st.vsc_laps) == (1, 2)
    assert st.red_flags == 0 and st.investigations == 1
    assert st.penalties == 1  # "PENALTY SERVED" isn't a second penalty
    assert rc["Q"].stats.red_flags == 1
    kinds = {m.message: session_data.kind(m) for m in rc["RACE"].messages}
    assert kinds["SAFETY CAR DEPLOYED"] == "sc"
    assert kinds["FIA STEWARDS: 10 SECOND TIME PENALTY FOR CAR 1 (VER) - CAUSING A COLLISION"] == (
        "stewards"
    )
    assert race.id != quali.id


def test_unknown_period_length_is_not_guessed():
    def m(minute, lap, text):
        return RaceControlMessage(
            session_id=1, at=datetime(2025, 1, 1, 12, minute, tzinfo=UTC), lap=lap, message=text
        )

    still_out = [m(1, 50, "SAFETY CAR DEPLOYED")]
    assert session_data.stats(still_out).sc_laps is None
    no_laps = [m(1, None, "SAFETY CAR DEPLOYED"), m(5, None, "SAFETY CAR IN THIS LAP")]
    st = session_data.stats(no_laps)
    assert st.safety_cars == 1 and st.sc_laps is None


def test_safety_car_history_and_last_year(db, mexico):
    _sync(db)
    hist = session_data.sc_history(db, mexico)
    assert (hist.races, hist.with_sc, hist.with_red, hist.seasons) == (2, 1, 1, [2024, 2023])
    assert hist.text == "SC in 1 of last 2 races here"
    ly = session_data.last_year_conditions(db, mexico)
    assert ly.event.season == 2024 and ly.conditions.rain is True
    assert (ly.conditions.track_min, ly.conditions.track_max) == (40.6, 51.7)


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _get(client, path, at):
    with time_machine.travel(at, tick=False):
        client.cookies.clear()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
        client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        return client.get(path).text


def test_event_page_shows_timeline_chart_and_last_year(db, client, mexico):
    _sync(db)
    save_settings(db, {"temp_unit": "C"})
    db.commit()
    html = _get(client, f"/events/{mexico.id}", NOW)
    assert "Race control" in html and "SAFETY CAR DEPLOYED" in html
    assert 'data-rc-filter="sc"' in html
    assert "41–52 °C" in html  # race track temperature range
    assert 'id="temp-chart-data"' in html and "/static/vendor/chart.umd.min.js" in html
    assert "Last year here" in html and "41–52 °C" in html
    assert "SC in 1 of last 2 races here" in html
    assert "<script>" not in html  # data travels as application/json, no inline JS


def test_temperature_unit_setting(db, client, mexico):
    _sync(db)
    save_settings(db, {"temp_unit": "F"})
    db.commit()
    html = _get(client, f"/events/{mexico.id}", NOW)
    assert "106–125 °F" in html and "41–52 °C" not in html
    save_settings(db, {"temp_unit": "both"})
    db.commit()
    assert "41–52 °C / 106–125 °F" in _get(client, f"/events/{mexico.id}", NOW)


def test_race_control_hidden_while_spoilers_held(db, client, mexico):
    _sync(db)
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    during_race = datetime(2025, 10, 26, 21, 0, tzinfo=UTC)
    html = _get(client, f"/events/{mexico.id}", during_race)
    assert "SAFETY CAR DEPLOYED" not in html and "RED FLAG" not in html
    assert "hidden by spoiler mode" in html
    revealed = _get(client, f"/events/{mexico.id}?reveal=1", during_race)
    assert "SAFETY CAR DEPLOYED" in revealed
    # A day later, still unwatched: still hidden.
    assert "SAFETY CAR DEPLOYED" not in _get(
        client, f"/events/{mexico.id}", during_race + timedelta(days=1)
    )


def test_safety_car_trivia_from_real_history(db, mexico):
    from pitwall.services import trivia

    for race in races("results_2024.json"):
        apply_results(db, race, "RACE")
    db.commit()
    assert not [f for f in trivia.generate(db, mexico) if f.type == "safety_car"]  # no data yet
    _sync(db)
    facts = {f.key: f for f in trivia.generate(db, mexico) if f.type == "safety_car"}
    sc = facts["sc:2025-rodriguez"]
    assert sc.text == (
        "There's been a safety car in 1 of the last 2 races at Autódromo Hermanos Rodríguez."
    )
    assert "OpenF1" in sc.source and "2023–2024" in sc.source
    assert facts["red:2025-rodriguez"].text.startswith("1 of the last 2 races")
