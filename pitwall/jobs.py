"""Scheduled job entry points. Each job is defensive: an API outage logs a warning and the
job returns, keeping whatever data is already cached. Jobs never raise into the scheduler."""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import select

from pitwall import scheduler as sched_mod
from pitwall.config import get_env
from pitwall.db import session_scope, utcnow
from pitwall.models import Event, Season, Session
from pitwall.notify import builders, outbox
from pitwall.services import circuit_maps, digest, openf1_sync, reminders, trivia, weather
from pitwall.services.sync import (
    apply_results,
    apply_schedule,
    apply_standings,
)
from pitwall.settings_store import get_kv, load_settings, set_kv
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.jolpica import JolpicaClient, make_client
from pitwall.sources.openf1 import OpenF1Client
from pitwall.sources.openf1 import make_client as make_openf1_client
from pitwall.sources.openmeteo import OpenMeteoClient
from pitwall.sources.openmeteo import make_client as make_openmeteo_client

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

RESULT_TYPES = ("Q", "SPRINT", "RACE")
RESULTS_START_AFTER = timedelta(hours=2)
RESULTS_GIVE_UP_AFTER = timedelta(hours=26)  # 2h delay + 24h of polling
_client_factory: Callable[[], JolpicaClient] = make_client


def set_client_factory(factory: Callable[[], JolpicaClient]) -> None:
    """Tests inject a client backed by recorded fixtures."""
    global _client_factory
    _client_factory = factory


def safe_job[F: Callable[..., Any]](fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("job failed", extra={"job": fn.__name__})
            return None

    return wrapper  # type: ignore[return-value]


def _reconcile_reminders(now: datetime | None = None) -> None:
    sched = sched_mod.get_scheduler()
    if sched is None:
        return
    with session_scope() as db:
        reminders.reconcile(sched, db, load_settings(db), now)


# ---------------------------------------------------------------------------- schedule


def sync_schedule(client: JolpicaClient, year: int, now: datetime | None = None) -> int:
    """Fetch + apply one season's calendar, queueing schedule-change alerts. Returns #changes."""
    now = now or utcnow()
    try:
        races = client.schedule(year)
    except SourceUnavailable:
        return 0
    with session_scope() as db:
        existing = db.scalar(
            select(Event.id).where(Event.season == year, Event.status == "scheduled").limit(1)
        )
        if not races:
            if existing is not None:
                log.warning("schedule came back empty; ignoring", extra={"year": year})
            return 0
        n_existing = (
            db.query(Event).filter(Event.season == year, Event.status == "scheduled").count()
        )
        if n_existing >= 6 and len(races) < n_existing // 2:
            log.warning(
                "schedule shrank suspiciously; ignoring this response",
                extra={"year": year, "was": n_existing, "now": len(races)},
            )
            return 0
        result = apply_schedule(db, year, races, now=now)
        settings = load_settings(db)
        for change in result.changes:
            outbox.enqueue(
                db, builders.schedule_change(db, change, settings), settings=settings, now=now
            )
        if result.changes:
            log.info(
                "schedule changes detected", extra={"year": year, "events": len(result.changes)}
            )
        return len(result.changes)


@safe_job
def schedule_sync_job() -> None:
    client = _client_factory()
    try:
        now = utcnow()
        for year in (now.year, now.year + 1):
            sync_schedule(client, year, now)
    finally:
        client.close()
    _reconcile_reminders()
    flush_job()


# ---------------------------------------------------------------------------- results


def due_result_sessions(now: datetime) -> list[tuple[int, int, int, str]]:
    """(session_id, season, round, type) for sessions in their results-polling window."""
    with session_scope() as db:
        rows = db.execute(
            select(Session.id, Event.season, Event.round, Session.type, Session.end_utc_est)
            .join(Event)
            .where(
                Session.type.in_(RESULT_TYPES),
                Session.status == "scheduled",
                Event.status == "scheduled",
                Session.results_fetched_at.is_(None),
                Session.end_utc_est.is_not(None),
            )
        ).all()
    return [
        (sid, season, rnd, stype)
        for sid, season, rnd, stype, end in rows
        if end + RESULTS_START_AFTER <= now <= end + RESULTS_GIVE_UP_AFTER
    ]


def _fetch_session_results(
    client: JolpicaClient, season: int, rnd: int, stype: str
) -> list[dict[str, Any]]:
    if stype == "Q":
        return client.qualifying(season, rnd)
    if stype == "SPRINT":
        return client.sprint(season, rnd)
    return client.results(season, rnd)


def update_standings(
    client: JolpicaClient, season: int, min_round: int, now: datetime | None = None
) -> bool:
    """Snapshot standings if the API has caught up to `min_round`. Returns True when done."""
    now = now or utcnow()
    try:
        driver_list = client.standings("driver", season)
        constructor_list = client.standings("constructor", season)
    except SourceUnavailable:
        return False
    if driver_list is None or int(driver_list["round"]) < min_round:
        return False
    with session_scope() as db:
        upd = apply_standings(db, "driver", driver_list, now=now)
        if constructor_list is not None:
            apply_standings(db, "constructor", constructor_list, now=now)
        event = db.scalars(
            select(Event).where(
                Event.season == season, Event.round == upd.round, Event.status == "scheduled"
            )
        ).first()
        msg = builders.standings(db, season, event)
        if msg is not None:
            outbox.enqueue(db, msg, now=now)
    return True


@safe_job
def results_sync_job(now: datetime | None = None) -> None:
    now = now or utcnow()
    due = due_result_sessions(now)
    with session_scope() as db:
        pending: list[dict[str, Any]] = get_kv(db, "standings_pending", [])
    if not due and not pending:
        return
    client = _client_factory()
    try:
        for _sid, season, rnd, stype in due:
            try:
                races = _fetch_session_results(client, season, rnd, stype)
            except SourceUnavailable:
                continue
            race = next((r for r in races if int(r["round"]) == rnd), None)
            if race is None:
                continue
            with session_scope() as db:
                sess = apply_results(db, race, stype, now=now)
                if sess is None:
                    continue
                settings = load_settings(db)
                msg = builders.results(db, sess, settings)
                if msg is not None:
                    outbox.enqueue(db, msg, settings=settings, now=now)
                log.info("results stored", extra={"season": season, "round": rnd, "session": stype})
            if stype in ("RACE", "SPRINT"):
                pending.append({"season": season, "round": rnd, "since": now.isoformat()})

        still: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for item in pending:
            key = (item["season"], item["round"])
            if key in seen:
                continue
            seen.add(key)
            if update_standings(client, item["season"], item["round"], now):
                continue
            if now - datetime.fromisoformat(item["since"]) < timedelta(hours=24):
                still.append(item)
        with session_scope() as db:
            set_kv(db, "standings_pending", still)
    finally:
        client.close()
    flush_job()


# ---------------------------------------------------------------------------- preview


def race_week_event(now: datetime) -> int | None:
    """The event whose race falls in the 7 days starting today (owner's local date)."""
    with session_scope() as db:
        settings = load_settings(db)
        tz = settings.tz
        start_local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_local
        end = start_local + timedelta(days=7)
        sess = db.scalars(
            select(Session)
            .join(Event)
            .where(
                Session.type == "RACE",
                Session.status == "scheduled",
                Event.status == "scheduled",
                Session.start_utc >= start,
                Session.start_utc < end,
            )
            .order_by(Session.start_utc)
        ).first()
        return sess.event_id if sess else None


@safe_job
def preview_job(now: datetime | None = None) -> None:
    now = now or utcnow()
    event_id = race_week_event(now)
    if event_id is None:
        log.info("no race this week; no preview")
        return
    weather_sync_job(now, event_id)  # freshest forecast for the preview
    with session_scope() as db:
        event = db.get(Event, event_id)
        assert event is not None
        settings = load_settings(db)
        send_preview(db, event, settings, now)
    flush_job()


def send_preview(
    db: Any, event: Event, settings: Any, now: datetime, dedup_content: str | None = None
) -> bool:
    """Queue the race-week preview and remember its trivia so later previews don't repeat it."""
    msg = builders.preview(db, event, settings, now)
    if dedup_content:
        msg.dedup_content = dedup_content
    row = outbox.enqueue(db, msg, settings=settings, now=now)
    if row is not None:
        trivia.remember_keys(db, [f["key"] for f in msg.data.get("facts", [])])
    return row is not None


# ------------------------------------------------------------------- weather / maps / digest

_weather_factory: Callable[[], OpenMeteoClient] = make_openmeteo_client


def set_weather_factory(factory: Callable[[], OpenMeteoClient]) -> None:
    global _weather_factory
    _weather_factory = factory


@safe_job
def weather_sync_job(now: datetime | None = None, event_id: int | None = None) -> None:
    client = _weather_factory()
    try:
        n = weather.sync(client, now, event_id)
    finally:
        client.close()
    if n:
        log.info("weather forecasts updated", extra={"sessions": n})


@safe_job
def circuit_maps_job(now: datetime | None = None) -> None:
    client = _openf1_factory()
    try:
        circuit_maps.generate_missing(client, now)
    finally:
        client.close()


@safe_job
def digest_job(now: datetime | None = None) -> None:
    now = now or utcnow()
    with _flush_lock, session_scope() as db:
        settings = load_settings(db)
        if not settings.digest_enabled:
            return
        built = digest.build(db, settings, now, get_env().base_url)
        if built is None:
            log.info("nothing for the weekly digest")
            return
        msg, rows = built
        if outbox.enqueue(db, msg, settings=settings, now=now) is not None:
            digest.mark_digested(db, rows, now)
    flush_job()


# ---------------------------------------------------------------------------- OpenF1


_openf1_factory: Callable[[], OpenF1Client] = make_openf1_client


def set_openf1_factory(factory: Callable[[], OpenF1Client]) -> None:
    global _openf1_factory
    _openf1_factory = factory


@safe_job
def openf1_sync_job(now: datetime | None = None) -> None:
    """Best lap / stints / pit stops for followed drivers (cheap no-op when nothing is due)."""
    now = now or utcnow()
    if not openf1_sync.pending_targets(now):
        return
    client = _openf1_factory()
    try:
        openf1_sync.sync(client, now)
    finally:
        client.close()


# ---------------------------------------------------------------------------- reminders


@safe_job
def send_reminder_job(session_id: int, offset_minutes: int, planned_for: str) -> None:
    now = utcnow()
    with session_scope() as db:
        sess = db.get(Session, session_id)
        if sess is None or sess.start_utc is None or sess.status != "scheduled":
            return
        expected = sess.start_utc - timedelta(minutes=offset_minutes)
        if abs((expected - datetime.fromisoformat(planned_for)).total_seconds()) > 60:
            log.info("stale reminder skipped (session moved)", extra={"session": session_id})
            return
        if now >= sess.start_utc:
            return
        settings = load_settings(db)
        outbox.enqueue(
            db, builders.reminder(sess, offset_minutes, settings, now), settings=settings, now=now
        )
    flush_job()


# ---------------------------------------------------------------------------- outbox


_flush_lock = threading.Lock()


@safe_job
def flush_job() -> None:
    # Serialised so the per-minute flush and a flush triggered from the UI can't both send the
    # same alert before either has recorded it.
    with _flush_lock, session_scope() as db:
        stats = outbox.flush(db)
    if stats["sent"] or stats["failed"] or stats["dropped"]:
        log.info("outbox flushed", extra=stats)


# ---------------------------------------------------------------------------- backfill


def _progress(**updates: Any) -> None:
    with session_scope() as db:
        state = get_kv(db, "backfill_progress", {}) or {}
        state.update(updates)
        set_kv(db, "backfill_progress", state)


def backfill_season(client: JolpicaClient, year: int, current_year: int, now: datetime) -> None:
    past = year < current_year
    max_age = None if past else timedelta(0)  # completed seasons never change
    if past:
        races = client.schedule(year, max_age=max_age)
        with session_scope() as db:
            apply_schedule(db, year, races, now=now, detect_changes=False)
    fetchers: list[tuple[str, Callable[..., list[dict[str, Any]]]]] = [
        ("RACE", client.results),
        ("Q", client.qualifying),
    ]
    if year >= 2021:
        fetchers.append(("SPRINT", client.sprint))
    for stype, fetch in fetchers:
        races = fetch(year, max_age=max_age)
        with session_scope() as db:
            for race in races:
                if not past:
                    # Leave sessions inside their results window to results_sync_job, which
                    # sends the alert; backfill only fills in quietly.
                    ev = db.scalars(
                        select(Event).where(
                            Event.season == year,
                            Event.round == int(race["round"]),
                            Event.status == "scheduled",
                        )
                    ).first()
                    sess = next((s for s in ev.sessions if s.type == stype), None) if ev else None
                    if (
                        sess is not None
                        and sess.results_fetched_at is None
                        and sess.end_utc_est
                        and now <= sess.end_utc_est + RESULTS_GIVE_UP_AFTER
                    ):
                        continue
                apply_results(db, race, stype, now=now)
    if not past:
        for kind in ("driver", "constructor"):
            latest = client.standings(kind, year)
            if latest is None:
                continue
            rnd = int(latest["round"])
            prev = client.standings(kind, year, rnd - 1) if rnd > 1 else None
            with session_scope() as db:
                if prev is not None:
                    apply_standings(db, kind, prev, now=now)
                apply_standings(db, kind, latest, now=now)
    else:
        with session_scope() as db:
            season = db.get(Season, year)
            if season is not None:
                season.backfilled = True


@safe_job
def backfill_job() -> None:
    now = utcnow()
    current = now.year
    with session_scope() as db:
        start_year = load_settings(db).backfill_start_year
        done_years = {s.year for s in db.scalars(select(Season).where(Season.backfilled))}
    years = [current] + [y for y in range(current - 1, start_year - 1, -1) if y not in done_years]
    _progress(
        running=True,
        total=len(years),
        done=0,
        current=None,
        started_at=now.isoformat(),
        finished_at=None,
        errors=[],
    )
    client = _client_factory()
    errors: list[str] = []
    try:
        with session_scope() as db:
            have_current = db.scalar(select(Event.id).where(Event.season == current).limit(1))
        if have_current is None:
            sync_schedule(client, current, now)
        for i, year in enumerate(years):
            _progress(current=year, done=i)
            try:
                backfill_season(client, year, current, now)
            except SourceUnavailable as exc:
                errors.append(f"{year}: {exc}")
                log.warning("backfill season skipped", extra={"year": year})
            except Exception as exc:
                errors.append(f"{year}: {type(exc).__name__}")
                log.exception("backfill season failed", extra={"year": year})
    finally:
        client.close()
        _progress(
            running=False,
            done=len(years),
            current=None,
            finished_at=utcnow().isoformat(),
            errors=errors[-10:],
        )
    log.info("backfill finished", extra={"years": len(years), "errors": len(errors)})
