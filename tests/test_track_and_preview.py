"""Track history stats, the race-week preview and backfill."""

from __future__ import annotations

import time_machine
from sqlalchemy import select

from pitwall import jobs
from pitwall.models import Event, FollowedDriver, Outbox, Result, Season
from pitwall.notify import builders
from pitwall.services import track_stats
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import load_settings
from tests.conftest import dt, load, races

CURATED = {
    "rodriguez": {
        "timezone": "America/Mexico_City",
        "length_km": 4.304,
        "laps": 71,
        "turns": 17,
        "lap_record": {"driver": "Valtteri Bottas", "time": "1:17.774", "year": 2021},
    }
}


def _history(db):
    for year in (2022, 2023, 2024):
        apply_schedule(
            db,
            year,
            races(f"schedule_{year}.json"),
            now=dt("2025-01-01T00:00:00"),
            detect_changes=False,
            curated=CURATED,
        )
        for race in races(f"results_{year}.json"):
            apply_results(db, race, "RACE")
        for race in races(f"qualifying_{year}.json"):
            apply_results(db, race, "Q")
    apply_schedule(
        db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated=CURATED
    )
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def test_track_stats_from_history(db):
    ev = _history(db)
    st = track_stats.compute(db, ev)
    assert st.last_winner.season == 2024 and st.last_winner.driver == "Carlos Sainz"
    assert st.last_winner.constructor == "Ferrari"
    assert st.last_pole.driver == "Carlos Sainz"
    assert st.seasons == (2022, 2024)
    # Active = drivers/teams in the latest season with results (2024 here).
    assert st.winningest_driver is not None
    # 2022 Verstappen pole->win, 2023 Leclerc pole / Verstappen won from P3, 2024 Sainz pole->win
    assert st.pole_to_win == (2, 3) and st.pole_to_win_pct == 67
    assert st.winner_avg_gain == (0.7, 3)


def test_winningest_counts_only_active_drivers(db):
    ev = _history(db)
    # Make 2025 the "active" season with only Sainz and Norris racing.
    for race in races("results_2025_3.json"):
        race = dict(
            race,
            Results=[r for r in race["Results"] if r["Driver"]["driverId"] in ("sainz", "norris")],
        )
        apply_results(db, race, "RACE")
    st = track_stats.compute(db, ev)
    assert st.winningest_driver.name == "Carlos Sainz" and st.winningest_driver.wins == 1


def test_track_stats_empty_without_history(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    ev = db.scalars(select(Event).where(Event.slug == "2025-albert_park")).one()
    st = track_stats.compute(db, ev)
    assert st.empty and st.last_winner is None and st.pole_to_win_pct is None


def test_preview_message(db):
    ev = _history(db)
    apply_standings(
        db,
        "driver",
        load("driver_standings_2025_2.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    apply_standings(
        db,
        "driver",
        load("driver_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    db.add(FollowedDriver(driver_id="leclerc"))
    db.flush()
    msg = builders.preview(db, ev, load_settings(db), now=dt("2025-10-20T13:00:00"))
    assert msg.type == "preview"
    assert msg.title == "🏎️ Race week: Mexico City Grand Prix"
    for needle in (
        "Autódromo Hermanos Rodríguez",
        "Mexico City, Mexico",
        "Race: Sun 26 Oct, 4:00 PM EDT / Sun 26 Oct, 2:00 PM CST",
        "Lap record: 1:17.774, Valtteri Bottas (2021)",
        "2024 winner: Carlos Sainz",
        "1. Lando Norris 65 pts ▲1",
        "Charles Leclerc: P3 (Mexico City GP 2024)",
        "https://pitwall.example/events/",
    ):
        assert needle in msg.text, needle
    assert "SPRINT WEEKEND" not in msg.html and "Pole → win" in msg.html
    assert msg.data["sessions"][-1] == {
        "session": "RACE",
        "start_utc": "2025-10-26T20:00:00+00:00",
        "odd_hours": False,
    }


def test_odd_hours_flag_in_preview(db):
    apply_schedule(
        db,
        2025,
        races("schedule_2025.json"),
        now=dt("2025-03-01T00:00:00"),
        curated={"albert_park": {"timezone": "Australia/Melbourne"}},
    )
    ev = db.scalars(select(Event).where(Event.slug == "2025-albert_park")).one()
    msg = builders.preview(db, ev, load_settings(db), now=dt("2025-03-10T13:00:00"))
    assert "Race starts at 12:00 AM your time" in msg.text  # 04:00Z = midnight EDT
    assert "Qualifying starts at 1:00 AM your time" in msg.text


def test_preview_job_picks_this_weeks_race_once(db, sent):
    _history(db)
    db.commit()
    monday = dt("2025-10-20T13:00:00")  # Mon 09:00 New York
    with time_machine.travel(monday, tick=False):
        jobs.preview_job(monday)
        jobs.preview_job(monday)
    previews = list(db.scalars(select(Outbox).where(Outbox.type == "preview")))
    assert len(previews) == 1 and "Mexico City" in previews[0].title
    assert sent.titles("email") == [previews[0].title]
    assert jobs.race_week_event(dt("2025-10-13T13:00:00")) is None


def test_backfill_is_polite_and_resumable(db, api):
    routes, _ = api
    for y in (2022, 2023, 2024):
        for kind in ("results", "qualifying", "sprint"):
            routes.set(f"{y}/{kind}.json", f"{kind}_{y}.json")
        routes.set(f"{y}.json", f"schedule_{y}.json")
    routes.set("2025.json", "schedule_2025.json")
    routes.set(
        "2025/results.json", {"MRData": {"total": "0", "limit": "100", "RaceTable": {"Races": []}}}
    )
    routes.set("2025/qualifying.json", "qualifying_2025_3.json")
    routes.set(
        "2025/sprint.json", {"MRData": {"total": "0", "limit": "100", "RaceTable": {"Races": []}}}
    )
    routes.set("2025/driverStandings.json", "driver_standings_2025_3.json")
    routes.set("2025/2/driverStandings.json", "driver_standings_2025_2.json")
    routes.set("2025/constructorStandings.json", "constructor_standings_2025_3.json")
    routes.set("2025/2/constructorStandings.json", "constructor_standings_2025_2.json")
    db.add(Season(year=2025))
    from pitwall.settings_store import save_settings

    save_settings(db, {"backfill_start_year": 2022})
    db.commit()
    with time_machine.travel(dt("2025-11-15T08:00:00"), tick=False):
        jobs.backfill_job()
    db.expire_all()
    assert {s.year for s in db.scalars(select(Season).where(Season.backfilled))} == {
        2022,
        2023,
        2024,
    }
    assert db.query(Result).count() > 10
    from pitwall.settings_store import get_kv

    prog = get_kv(db, "backfill_progress")
    assert prog["running"] is False and prog["done"] == prog["total"] == 4
    calls_before = len(routes.calls)
    with time_machine.travel(dt("2025-11-16T08:00:00"), tick=False):
        jobs.backfill_job()
    new_calls = routes.calls[calls_before:]
    assert all(c.startswith("2025") for c in new_calls)  # completed seasons not re-fetched
