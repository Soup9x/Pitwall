"""Spoiler mode: one place that decides whether an event's results are still held for a user.

Both the alert queue (pitwall.notify.outbox) and the web pages use `held()`, so an event is
hidden on the pages exactly as long as its results/standings alerts are held.

An event is held while spoiler mode is on, the user hasn't marked it watched and - when
auto-release is set - fewer than N hours have passed since its latest results arrived."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import OWNER_ID, Event, Session, SpoilerState
from pitwall.settings_store import AppSettings


def mark_watched(
    db: DB,
    event_id: int,
    watched: bool = True,
    *,
    user_id: int = OWNER_ID,
    now: datetime | None = None,
) -> None:
    state = db.get(SpoilerState, (user_id, event_id))
    if state is None:
        state = SpoilerState(user_id=user_id, event_id=event_id)
        db.add(state)
    state.watched = watched
    state.watched_at = (now or utcnow()) if watched else None
    db.flush()


def is_watched(db: DB, event_id: int, user_id: int = OWNER_ID) -> bool:
    state = db.get(SpoilerState, (user_id, event_id))
    return bool(state and state.watched)


def results_arrived_at(db: DB, event_id: int) -> datetime | None:
    """When the event's most recent results came in (None if none have yet). A new session's
    results restart the auto-release clock, so a race isn't revealed by its qualifying clock."""
    return db.scalar(
        select(func.max(Session.results_fetched_at)).where(Session.event_id == event_id)
    )


def held(
    db: DB,
    event_id: int | None,
    user_id: int,
    settings: AppSettings,
    now: datetime | None = None,
    *,
    since: datetime | None = None,
) -> bool:
    """True while the event's spoilers must stay hidden from this user.

    The auto-release clock starts at the event's latest results, or at `since` if that is
    later (an alert created after the results, e.g. a standings update, waits its full time)."""
    if not settings.spoiler_enabled or event_id is None:
        return False
    if is_watched(db, event_id, user_id):
        return False
    hours = settings.spoiler_auto_release_hours
    if not hours or hours <= 0:
        return True
    starts = [t for t in (results_arrived_at(db, event_id), since) if t is not None]
    if not starts:
        return True
    return (now or utcnow()) < max(starts) + timedelta(hours=hours)


def latest_results_event(db: DB) -> Event | None:
    """The event with the most recent session that has results (a sprint or qualifying counts:
    a sprint changes the standings before the race)."""
    return db.scalars(
        select(Event)
        .join(Session, Session.event_id == Event.id)
        .where(Session.results_fetched_at.is_not(None))
        .order_by(Session.start_utc.desc())
        .limit(1)
    ).first()


def held_event(
    db: DB, user_id: int, settings: AppSettings, now: datetime | None = None
) -> Event | None:
    """The latest event with results, if it's held for this user. While it is, anything derived
    from the latest results (standings, form, head-to-head, trivia) must stay hidden too."""
    if not settings.spoiler_enabled:
        return None
    event = latest_results_event(db)
    return event if event is not None and held(db, event.id, user_id, settings, now) else None
