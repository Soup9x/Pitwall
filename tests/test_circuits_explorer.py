"""Circuit explorer: the circuit list and a circuit's full race history, grouped by circuit,
with the computed fastest lap kept apart from the official record, and spoiler-safe."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import (
    Event,
    RaceControlMessage,
    SessionConditions,
    SessionDataFetch,
)
from pitwall.services import circuits_explorer
from pitwall.services.sync import apply_results, apply_schedule
from pitwall.settings_store import save_settings
from pitwall.web import auth
from tests.conftest import dt, races

NOW = dt("2025-10-28T12:00:00")
RACE_AT = dt("2025-10-26T22:00:00")


@pytest.fixture
def history(db):
    for year in (2022, 2023, 2024):
        apply_schedule(
            db, year, races(f"schedule_{year}.json"), now=dt(f"{year}-03-01T00:00:00"), curated={}
        )
        for race in races(f"results_{year}.json"):
            apply_results(db, race, "RACE", now=dt(f"{year}-11-01T00:00:00"))
        for race in races(f"qualifying_{year}.json"):
            apply_results(db, race, "Q", now=dt(f"{year}-11-01T00:00:00"))
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for race in races("qualifying_2025_3.json"):
        apply_results(db, race, "Q", now=RACE_AT - timedelta(days=1))
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=RACE_AT)
    db.commit()


def _race(db, season):
    ev = db.scalars(
        select(Event).where(Event.season == season, Event.circuit_id == "rodriguez")
    ).one()
    return next(s for s in ev.sessions if s.type == "RACE")


def test_listing_puts_the_current_calendar_first(db, history):
    rows = circuits_explorer.listing(db, 2025, NOW)
    assert [r.circuit.id for r in rows] == ["albert_park", "shanghai", "rodriguez"]
    mex = rows[2]
    assert (mex.current_round, mex.races, mex.first, mex.last) == (3, 4, 2022, 2025)
    found = circuits_explorer.listing(db, 2025, NOW, query="mexico")
    assert [r.circuit.id for r in found] == ["rodriguez"]
    assert circuits_explorer.listing(db, 2025, NOW, which="historical") == []
    # A circuit that isn't on this year's calendar counts as historical.
    old = circuits_explorer.listing(db, 2026, NOW, which="historical")
    assert {r.circuit.id for r in old} == {"albert_park", "shanghai", "rodriguez"}


def test_detail_history_and_patterns(db, history):
    c = circuits_explorer.detail(db, "rodriguez", 2025, NOW)
    assert [r.event.season for r in c.races] == [2025, 2024, 2023, 2022]
    assert c.current.season == 2025 and c.last_held.season == 2025
    by_year = {r.event.season: r for r in c.races}
    assert by_year[2023].winner.driver_id == "max_verstappen"
    assert by_year[2023].pole.driver_id == "leclerc"  # from qualifying, not the race grid
    assert by_year[2023].fastest.driver_id == "max_verstappen"
    assert by_year[2022].fastest is None  # not in the data: left blank
    assert [(w.id, w.count, w.active) for w in c.wins][:1] == [("max_verstappen", 2, True)]
    assert c.poles[0].count == 1
    assert c.team_wins[0].id == "red_bull" and c.team_wins[0].count == 2
    assert c.pole_to_win == (3, 4) and c.pole_to_win_pct == 75
    assert c.winner_grid == (1.5, 4)
    assert c.dnf == (1, 15)  # Sainz retired in 2025
    # Same 1:21.000 in three years: the first time it was set is the one shown.
    assert (c.fastest_lap.time, c.fastest_lap.season, c.fastest_lap.driver.id) == (
        "1:21.000",
        2023,
        "max_verstappen",
    )


def test_race_control_and_conditions_history(db, history):
    race = _race(db, 2024)
    db.add(SessionDataFetch(session_id=race.id, race_control_count=2, weather_samples=10))
    for minute, text in ((10, "SAFETY CAR DEPLOYED"), (20, "SAFETY CAR IN THIS LAP")):
        db.add(
            RaceControlMessage(
                session_id=race.id,
                at=dt("2024-10-27T20:00:00") + timedelta(minutes=minute),
                lap=minute // 5,
                message=text,
            )
        )
    db.add(
        SessionConditions(
            session_id=race.id,
            samples=10,
            track_min=40.0,
            track_max=48.0,
            air_min=20.0,
            air_max=23.0,
            rain=False,
        )
    )
    db.commit()
    c = circuits_explorer.detail(db, "rodriguez", 2025, NOW)
    assert [(s.season, s.safety_cars, s.red_flags) for s in c.sc] == [(2024, 1, 0)]
    assert [w.season for w in c.weather] == [2024]


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _get(client, path, at=NOW):
    with time_machine.travel(at, tick=False):
        client.cookies.clear()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
        client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        return client.get(path)


def test_pages(client, db, history):
    listing = _get(client, "/circuits?q=mex").text
    assert 'href="/circuits/rodriguez"' in listing and "Round 3" in listing
    assert "albert_park" not in listing
    page = _get(client, "/circuits/rodriguez").text
    assert "Fastest race lap in Pitwall's data" in page and "Official race lap record" in page
    assert "It is never the record" in page
    assert "unverified" in page  # curated facts not verified yet
    assert "2025 · Round 3" in page and "Every race here" in page
    assert "75%" in page and "6.7%" in page
    assert _get(client, "/circuits/nowhere").status_code == 404
    event = db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()
    assert 'href="/circuits/rodriguez"' in _get(client, f"/events/{event.id}").text


def test_held_race_is_left_out(client, db, history):
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    soon = RACE_AT + timedelta(hours=2)
    page = _get(client, "/circuits/rodriguez", soon).text
    assert '<td class="num">2025</td>' not in page  # no 2025 row
    assert "/drivers/norris?season=2025" not in page  # ...and no 2025 winner anywhere
    assert "is left out of this page" in page and "?reveal=1" in page
    assert "Max Verstappen" in page  # earlier years are still shown
    assert '<td class="num">2025</td>' in _get(client, "/circuits/rodriguez?reveal=1", soon).text
    with time_machine.travel(soon, tick=False):
        c = circuits_explorer.detail(db, "rodriguez", 2025, soon, hidden={_race(db, 2025).event_id})
    assert [r.event.season for r in c.races] == [2024, 2023, 2022]
