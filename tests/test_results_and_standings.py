"""Results polling window, results/standings alerts and standings deltas."""

from __future__ import annotations

import time_machine
from sqlalchemy import select

from pitwall import jobs
from pitwall.models import Event, Outbox, Result, Session, StandingsSnapshot
from pitwall.services import standings
from pitwall.services.sync import apply_schedule, apply_standings
from tests.conftest import dt, load, races

RACE_END = dt("2025-10-26T22:00:00")


def _setup(db, routes):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    apply_standings(
        db,
        "driver",
        load("driver_standings_2025_2.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    apply_standings(
        db,
        "constructor",
        load("constructor_standings_2025_2.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    # Earlier rounds already have results; only Mexico is due.
    for s in db.scalars(select(Session)):
        if s.event.round < 3:
            s.results_fetched_at = dt("2025-04-01T00:00:00")
    db.commit()
    routes.set("2025/3/qualifying.json", "qualifying_2025_3.json")
    routes.set("2025/3/results.json", "results_2025_3_empty.json")
    routes.set("2025/driverStandings.json", "driver_standings_2025_2.json")
    routes.set("2025/constructorStandings.json", "constructor_standings_2025_2.json")


def test_polling_window(db, api):
    routes, _ = api
    _setup(db, routes)
    # Race just ended: polling for it starts 2h later (quali is still inside its window).
    assert {t for *_x, t in jobs.due_result_sessions(RACE_END)} == {"Q"}
    due = {t for *_x, t in jobs.due_result_sessions(dt("2025-10-27T00:00:00"))}
    assert due == {"Q", "RACE"}  # quali ended 22:00 Sat (+26h window), race ends 22:00 Sun
    assert {t for *_x, t in jobs.due_result_sessions(dt("2025-10-26T23:59:00"))} == {"Q"}
    assert jobs.due_result_sessions(dt("2025-10-28T00:01:00")) == []  # gave up after 26h


def test_results_flow_with_alerts_and_standings(db, api, sent):
    routes, _ = api
    _setup(db, routes)

    # 2h after race end: quali results exist, race results not yet published.
    with time_machine.travel(dt("2025-10-27T00:00:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T00:00:00"))
    db.expire_all()
    q = db.scalars(select(Session).join(Event).where(Event.round == 3, Session.type == "Q")).one()
    race = db.scalars(
        select(Session).join(Event).where(Event.round == 3, Session.type == "RACE")
    ).one()
    assert q.results_fetched_at is not None and race.results_fetched_at is None
    assert sent.titles() == ["⏱️ Qualifying results — Mexico City GP"]

    # 30 min later the race results and updated standings land.
    routes.set("2025/3/results.json", "results_2025_3.json")
    routes.set("2025/driverStandings.json", "driver_standings_2025_3.json")
    routes.set("2025/constructorStandings.json", "constructor_standings_2025_3.json")
    with time_machine.travel(dt("2025-10-27T00:30:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T00:30:00"))
    db.expire_all()
    rows = list(
        db.scalars(select(Result).where(Result.session_id == race.id).order_by(Result.position))
    )
    assert rows[0].driver_id == "norris" and rows[0].fastest_lap_rank == 1
    assert rows[-1].position_text == "R" and rows[-1].status == "Retired"
    titles = sent.titles()
    assert "🏁 Race results — Mexico City GP" in titles
    assert "🏆 Championship standings after Mexico City GP" in titles

    # Nothing is sent twice on the next poll.
    n = len(sent.items)
    with time_machine.travel(dt("2025-10-27T01:00:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T01:00:00"))
    assert len(sent.items) == n


def test_standings_alert_waits_for_api_to_catch_up(db, api, sent):
    routes, _ = api
    _setup(db, routes)
    routes.set("2025/3/results.json", "results_2025_3.json")
    with time_machine.travel(dt("2025-10-27T00:00:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T00:00:00"))
    assert not any("standings" in t for t in sent.titles())
    db.expire_all()
    from pitwall.settings_store import get_kv

    assert get_kv(db, "standings_pending")[0]["round"] == 3
    routes.set("2025/driverStandings.json", "driver_standings_2025_3.json")
    routes.set("2025/constructorStandings.json", "constructor_standings_2025_3.json")
    with time_machine.travel(dt("2025-10-27T00:30:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T00:30:00"))
    assert any("standings" in t for t in sent.titles())
    db.expire_all()
    assert get_kv(db, "standings_pending") == []


def test_standings_deltas_and_gaps(db):
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    table = standings.table(db, 2025, "driver")
    assert table.round == 3
    by = {r.entity_id: r for r in table.rows}
    assert by["norris"].change == 1 and by["norris"].arrow == "▲1"
    assert by["piastri"].change == -1 and by["piastri"].arrow == "▼1"
    assert by["max_verstappen"].change == 0 and by["max_verstappen"].arrow == "–"
    assert by["norris"].gap_leader is None and by["norris"].gap_ahead is None
    assert by["piastri"].gap_leader == 9 and by["piastri"].gap_ahead == 9
    assert by["max_verstappen"].gap_leader == 14 and by["max_verstappen"].gap_ahead == 5
    first = standings.table(db, 2025, "driver", 2)
    assert all(r.change is None for r in first.rows)


def test_standings_snapshot_upsert_reports_changes(db):
    lst = load("driver_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0]
    assert apply_standings(db, "driver", lst).changed
    assert not apply_standings(db, "driver", lst).changed
    assert db.query(StandingsSnapshot).count() == 5


def test_results_alert_held_by_spoiler_mode(db, api, sent):
    from pitwall.settings_store import save_settings

    routes, _ = api
    _setup(db, routes)
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    with time_machine.travel(dt("2025-10-27T00:00:00"), tick=False):
        jobs.results_sync_job(dt("2025-10-27T00:00:00"))
    assert sent.items == []
    db.expire_all()
    held = list(db.scalars(select(Outbox).where(Outbox.hold_reason == "spoiler")))
    assert len(held) == 1 and held[0].type == "results"
