"""Session reminders as individual APScheduler 'date' jobs.

`reconcile()` computes the reminders that *should* exist from session start times and the
configured offsets, then adds/moves/removes scheduler jobs to match. It runs after every
schedule sync and every settings change, so a moved session always has its reminders moved."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import Event, Session
from pitwall.settings_store import AppSettings

log = logging.getLogger(__name__)

PREFIX = "reminder:"
SEND_FUNC = "pitwall.jobs:send_reminder_job"


@dataclass(frozen=True)
class PlannedReminder:
    job_id: str
    run_at: datetime
    session_id: int
    offset_minutes: int


def job_id(session_id: int, offset_minutes: int) -> str:
    return f"{PREFIX}{session_id}:{offset_minutes}"


def plan(db: DB, settings: AppSettings, now: datetime | None = None) -> dict[str, PlannedReminder]:
    now = now or utcnow()
    out: dict[str, PlannedReminder] = {}
    sessions = db.scalars(
        select(Session)
        .join(Event)
        .where(Session.start_utc > now, Session.status == "scheduled", Event.status == "scheduled")
    )
    for sess in sessions:
        assert sess.start_utc is not None
        for offset in settings.reminder_offsets.get(sess.type, []):
            run_at = sess.start_utc - timedelta(minutes=int(offset))
            if run_at <= now:
                continue
            jid = job_id(sess.id, int(offset))
            out[jid] = PlannedReminder(jid, run_at, sess.id, int(offset))
    return out


def reconcile(
    scheduler: Any, db: DB, settings: AppSettings, now: datetime | None = None
) -> dict[str, int]:
    """Make the scheduler's reminder jobs match the plan. Returns counts for logging/tests."""
    desired = plan(db, settings, now)
    existing = {j.id: j for j in scheduler.get_jobs() if j.id.startswith(PREFIX)}
    stats = {"added": 0, "moved": 0, "removed": 0, "kept": 0}
    for jid, _job in existing.items():
        if jid not in desired:
            scheduler.remove_job(jid)
            stats["removed"] += 1
    for jid, p in desired.items():
        current = existing.get(jid)
        if current is not None and current.next_run_time == p.run_at:
            stats["kept"] += 1
            continue
        scheduler.add_job(
            SEND_FUNC,
            trigger="date",
            run_date=p.run_at,
            id=jid,
            replace_existing=True,
            args=[p.session_id, p.offset_minutes, p.run_at.isoformat()],
            misfire_grace_time=max(60, min(p.offset_minutes * 60 // 2, 6 * 3600)),
            name=f"Reminder {p.offset_minutes}m before session {p.session_id}",
        )
        stats["moved" if current is not None else "added"] += 1
    if any(stats[k] for k in ("added", "moved", "removed")):
        log.info("reminders reconciled", extra=stats)
    return stats
