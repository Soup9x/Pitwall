"""Reminder planning and rescheduling when session times change."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
import time_machine
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from pitwall.models import Event, Outbox, Session
from pitwall.services import reminders
from pitwall.services.sync import apply_schedule
from pitwall.settings_store import load_settings, save_settings
from tests.conftest import dt, races

NOW = dt("2025-03-01T12:00:00")


@pytest.fixture
def sched():
    s = BackgroundScheduler(jobstores={"default": MemoryJobStore()}, timezone="UTC")
    s.start(paused=True)
    yield s
    s.shutdown(wait=False)


def _session(db, slug, stype):
    ev = db.scalars(select(Event).where(Event.slug == slug)).one()
    return next(s for s in ev.sessions if s.type == stype)


def test_default_plan_is_24h_and_1h_before_quali_and_race(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    plan = reminders.plan(db, load_settings(db), NOW)
    race = _session(db, "2025-rodriguez", "RACE")
    assert plan[reminders.job_id(race.id, 1440)].run_at == race.start_utc - timedelta(hours=24)
    assert plan[reminders.job_id(race.id, 60)].run_at == race.start_utc - timedelta(hours=1)
    types = {db.get(Session, p.session_id).type for p in plan.values()}
    assert types == {"Q", "RACE"}
    assert len(plan) == 3 * 2 * 2  # 3 events x (Q, RACE) x 2 offsets


def test_offsets_configurable_per_session_type(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    offsets = load_settings(db).reminder_offsets
    offsets.update({"SPRINT": [30], "RACE": [120], "Q": []})
    settings = save_settings(db, {"reminder_offsets": offsets})
    plan = reminders.plan(db, settings, NOW)
    by_type = sorted(
        {(db.get(Session, p.session_id).type, p.offset_minutes) for p in plan.values()}
    )
    assert by_type == [("RACE", 120), ("SPRINT", 30)]


def test_reconcile_adds_moves_and_removes_jobs(db, sched):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    settings = load_settings(db)
    stats = reminders.reconcile(sched, db, settings, NOW)
    assert stats["added"] == 12
    race = _session(db, "2025-rodriguez", "RACE")
    job = sched.get_job(reminders.job_id(race.id, 60))
    assert job.next_run_time == dt("2025-10-26T19:00:00")

    # Race moves one hour later -> both its reminders move; nothing else changes.
    moved = copy.deepcopy(data)
    moved[2]["time"] = "21:00:00Z"
    apply_schedule(db, 2025, moved, now=NOW, curated={})
    stats = reminders.reconcile(sched, db, settings, NOW)
    assert stats == {"added": 0, "moved": 2, "removed": 0, "kept": 10}
    job = sched.get_job(reminders.job_id(race.id, 60))
    assert job.next_run_time == dt("2025-10-26T20:00:00")
    assert job.args[2] == "2025-10-26T20:00:00+00:00"

    # Qualifying cancelled -> its reminders are removed.
    cancelled = copy.deepcopy(moved)
    del cancelled[2]["Qualifying"]
    apply_schedule(db, 2025, cancelled, now=NOW, curated={})
    stats = reminders.reconcile(sched, db, settings, NOW)
    assert stats["removed"] == 2
    q = _session(db, "2025-rodriguez", "Q")
    assert sched.get_job(reminders.job_id(q.id, 60)) is None


def test_past_reminders_not_scheduled(db, sched):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    later = dt("2025-10-26T19:30:00")  # 30 min before the Mexico race
    plan = reminders.plan(db, load_settings(db), later)
    assert plan == {}


def test_stale_reminder_job_is_skipped(db, sent):
    from pitwall import jobs

    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    race = _session(db, "2025-rodriguez", "RACE")
    db.commit()
    with time_machine.travel(dt("2025-10-26T19:00:00"), tick=False):
        jobs.send_reminder_job(race.id, 60, "2025-10-26T18:00:00+00:00")  # planned for old time
        assert db.scalars(select(Outbox)).first() is None
        jobs.send_reminder_job(race.id, 60, "2025-10-26T19:00:00+00:00")
    db.expire_all()
    row = db.scalars(select(Outbox)).one()
    assert row.type == "reminder" and row.title.startswith("🏁 Race in 1 hour")
    assert row.expires_at == race.start_utc
    assert sent.titles("webhook") == [row.title]
