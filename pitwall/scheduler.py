"""APScheduler setup with a persistent job store in the app's SQLite DB."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from pitwall.db import get_engine, session_scope, utcnow
from pitwall.settings_store import get_kv, load_settings

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def create_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(engine=get_engine(), tablename="apscheduler_jobs")
        },
        executors={"default": ThreadPoolExecutor(4)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        timezone="UTC",
    )


def install_recurring_jobs(sched: Any) -> None:
    with session_scope() as db:
        settings = load_settings(db)
        backfill_done = bool((get_kv(db, "backfill_progress") or {}).get("finished_at"))
    tz = settings.tz
    now = utcnow()
    sched.add_job(
        "pitwall.jobs:schedule_sync_job",
        "interval",
        hours=6,
        id="schedule_sync",
        replace_existing=True,
        next_run_time=now + timedelta(seconds=15),
        name="Schedule sync (every 6h)",
    )
    sched.add_job(
        "pitwall.jobs:results_sync_job",
        "interval",
        minutes=30,
        id="results_sync",
        replace_existing=True,
        next_run_time=now + timedelta(minutes=2),
        name="Results/standings poll (every 30 min)",
    )
    sched.add_job(
        "pitwall.jobs:flush_job",
        "interval",
        minutes=1,
        id="outbox_flush",
        replace_existing=True,
        name="Deliver queued alerts (every minute)",
    )
    sched.add_job(
        "pitwall.jobs:preview_job",
        "cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        timezone=tz,
        id="race_week_preview",
        replace_existing=True,
        name="Race-week preview (Mon 09:00 local)",
    )
    sched.add_job(
        "pitwall.jobs:backfill_job",
        "cron",
        hour=3,
        minute=30,
        timezone=tz,
        id="backfill_nightly",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        name="Historical backfill (nightly)",
    )
    sched.add_job(
        "pitwall.jobs:openf1_sync_job",
        "interval",
        hours=2,
        id="openf1_sync",
        replace_existing=True,
        next_run_time=now + timedelta(minutes=3),
        name="OpenF1 detail for followed drivers (every 2h)",
    )
    sched.add_job(
        "pitwall.jobs:weather_sync_job",
        "interval",
        hours=3,
        id="weather_sync",
        replace_existing=True,
        next_run_time=now + timedelta(minutes=1),
        name="Weather forecasts (every 3h)",
    )
    sched.add_job(
        "pitwall.jobs:circuit_maps_job",
        "cron",
        hour=4,
        minute=15,
        timezone=tz,
        id="circuit_maps",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        name="Draw missing circuit maps (nightly)",
    )
    if settings.digest_enabled:
        hh, mm = settings.digest_time.split(":")
        sched.add_job(
            "pitwall.jobs:digest_job",
            "cron",
            day_of_week=settings.digest_day,
            hour=int(hh),
            minute=int(mm),
            timezone=tz,
            id="weekly_digest",
            replace_existing=True,
            misfire_grace_time=6 * 3600,
            name="Weekly digest",
        )
    elif sched.get_job("weekly_digest") is not None:
        sched.remove_job("weekly_digest")
    if not backfill_done:
        sched.add_job(
            "pitwall.jobs:backfill_job",
            "date",
            run_date=now + timedelta(seconds=45),
            id="backfill_initial",
            replace_existing=True,
            name="Historical backfill (first run)",
        )


def start() -> BackgroundScheduler:
    global _scheduler
    sched = create_scheduler()
    sched.start(paused=True)
    install_recurring_jobs(sched)
    sched.resume()
    _scheduler = sched
    log.info("scheduler started", extra={"jobs": len(sched.get_jobs())})
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_now(job_id: str) -> bool:
    sched = get_scheduler()
    if sched is None or sched.get_job(job_id) is None:
        return False
    sched.modify_job(job_id, next_run_time=utcnow())
    return True


def state() -> dict[str, Any]:
    sched = get_scheduler()
    if sched is None:
        return {"running": False, "jobs": 0}
    jobs = sched.get_jobs()
    upcoming = sorted((j for j in jobs if j.next_run_time), key=lambda j: j.next_run_time)
    return {
        "running": bool(sched.running),
        "jobs": len(jobs),
        "reminders": sum(1 for j in jobs if j.id.startswith("reminder:")),
        "next": [{"id": j.id, "at": j.next_run_time.isoformat()} for j in upcoming[:5]],
    }
