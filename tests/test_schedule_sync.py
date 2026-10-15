"""Schedule sync and change detection."""

from __future__ import annotations

import copy

from sqlalchemy import select

from pitwall.models import Event, Outbox, Session
from pitwall.services.sync import apply_schedule
from tests.conftest import dt, load, races

NOW = dt("2025-03-01T12:00:00")  # before round 1


def _sessions(db, slug):
    ev = db.scalars(select(Event).where(Event.slug == slug)).one()
    return ev, {s.type: s for s in ev.sessions}


def test_first_import_creates_events_without_alerts(db):
    res = apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    assert res.first_import and res.changes == []
    ev, sess = _sessions(db, "2025-shanghai")
    assert ev.is_sprint_weekend
    assert set(sess) == {"FP1", "SQ", "SPRINT", "Q", "RACE"}
    assert sess["RACE"].start_utc == dt("2025-03-23T07:00:00")
    assert sess["RACE"].end_utc_est == dt("2025-03-23T09:00:00")
    ev, sess = _sessions(db, "2025-albert_park")
    assert not ev.is_sprint_weekend
    assert set(sess) == {"FP1", "FP2", "FP3", "Q", "RACE"}
    assert all(s.sequence == 0 for s in sess.values())


def test_curated_facts_merged(db):
    curated = {
        "rodriguez": {
            "timezone": "America/Mexico_City",
            "length_km": 4.304,
            "laps": 71,
            "lap_record": {"driver": None, "time": None, "year": None},
        }
    }
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated=curated)
    ev, _ = _sessions(db, "2025-rodriguez")
    assert ev.circuit.tz_name == "America/Mexico_City"
    assert ev.circuit.length_km == 4.304
    assert ev.circuit.turns is None  # missing -> null, never guessed


def test_moved_session_detected_and_sequence_bumped(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    changed = copy.deepcopy(data)
    changed[2]["Qualifying"]["time"] = "22:00:00Z"
    res = apply_schedule(db, 2025, changed, now=NOW, curated={})
    assert len(res.changes) == 1
    ch = res.changes[0]
    assert ch.kind == "changed"
    assert [(c.kind, c.session_type) for c in ch.sessions] == [("moved", "Q")]
    assert ch.sessions[0].old_start == dt("2025-10-25T21:00:00")
    assert ch.sessions[0].new_start == dt("2025-10-25T22:00:00")
    _, sess = _sessions(db, "2025-rodriguez")
    assert sess["Q"].sequence == 1 and sess["RACE"].sequence == 0


def test_unchanged_resync_reports_nothing(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    assert apply_schedule(db, 2025, data, now=NOW, curated={}).changes == []


def test_session_added_and_cancelled(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    changed = copy.deepcopy(data)
    del changed[0]["ThirdPractice"]
    changed[2]["Sprint"] = {"date": "2025-10-25", "time": "16:00:00Z"}
    res = apply_schedule(db, 2025, changed, now=NOW, curated={})
    kinds = {(c.kind, s.kind, s.session_type) for c in res.changes for s in c.sessions}
    assert ("changed", "cancelled", "FP3") in kinds
    assert ("changed", "added", "SPRINT") in kinds
    _, sess = _sessions(db, "2025-albert_park")
    assert sess["FP3"].status == "cancelled" and sess["FP3"].sequence == 1
    ev, _ = _sessions(db, "2025-rodriguez")
    assert ev.is_sprint_weekend


def test_event_cancelled_and_rounds_renumbered_without_false_alarms(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    changed = copy.deepcopy(data)
    del changed[1]  # China cancelled; Mexico becomes round 2
    changed[1]["round"] = "2"
    res = apply_schedule(db, 2025, changed, now=NOW, curated={})
    assert [(c.kind) for c in res.changes] == ["cancelled"]
    china, sess = _sessions(db, "2025-shanghai")
    assert china.status == "cancelled"
    assert all(s.status == "cancelled" for s in sess.values())
    mexico, msess = _sessions(db, "2025-rodriguez")
    assert mexico.round == 2 and all(s.sequence == 0 for s in msess.values())


def test_changes_to_past_sessions_are_not_alerted(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    changed = copy.deepcopy(data)
    changed[0]["time"] = "05:00:00Z"
    later = dt("2025-04-01T00:00:00")
    res = apply_schedule(db, 2025, changed, now=later, curated={})
    assert res.changes == []
    _, sess = _sessions(db, "2025-albert_park")
    assert sess["RACE"].start_utc == dt("2025-03-16T05:00:00")  # still stored


def test_sync_job_queues_schedule_change_alert(db, api, sent):
    from pitwall import jobs

    routes, factory = api
    routes.set("2025.json", "schedule_2025.json")
    assert jobs.sync_schedule(factory(), 2025, NOW) == 0
    moved = load("schedule_2025.json")
    moved["MRData"]["RaceTable"]["Races"][2]["time"] = "21:00:00Z"
    routes.set("2025.json", moved)
    assert jobs.sync_schedule(factory(), 2025, NOW) == 1
    db.expire_all()
    rows = list(db.scalars(select(Outbox).where(Outbox.type == "schedule_change")))
    assert len(rows) == 1
    assert "Mexico City Grand Prix" in rows[0].title
    assert "Race: moved from" in rows[0].text


def test_empty_or_shrunken_schedule_is_ignored(db, api):
    from pitwall import jobs

    routes, factory = api
    routes.set("2025.json", "schedule_2025.json")
    jobs.sync_schedule(factory(), 2025, NOW)
    empty = load("schedule_2025.json")
    empty["MRData"]["RaceTable"]["Races"] = []
    empty["MRData"]["total"] = "0"
    routes.set("2025.json", empty)
    assert jobs.sync_schedule(factory(), 2025, NOW) == 0
    db.expire_all()
    assert all(e.status == "scheduled" for e in db.scalars(select(Event)))


def test_api_outage_keeps_existing_data(db, api):
    from pitwall import jobs

    routes, factory = api
    routes.set("2025.json", 503)
    assert jobs.sync_schedule(factory(), 2025, NOW) == 0
    assert db.scalars(select(Session)).first() is None
