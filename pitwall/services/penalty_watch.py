"""Polls FIA event documents during race week and raises penalty-watch alerts.

Schedule (track-local time): every 2 hours from Wednesday to race day of race week, every 30
minutes on Friday and Saturday. Outside race week nothing is fetched.

Alerts:
* penalty     - a grid penalty is announced (parsed or entered by an admin)
* licence     - a driver's 12-month licence points reach 10 or 11 (or 12: race ban)
* pu_element  - a followed driver takes a new power unit element (off by default)

Anything published during or after the event's first competitive session counts as
post-session news, so spoiler mode holds it like results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import session_scope, utcnow
from pitwall.models import (
    Driver,
    Event,
    FiaDocument,
    FollowedDriver,
    GridPenalty,
    PuUsage,
    Session,
)
from pitwall.notify.message import Message
from pitwall.services import fia_docs, penalties, spoilers
from pitwall.services.presenters import event_url
from pitwall.settings_store import AppSettings, get_kv, set_kv
from pitwall.sources.fia import FiaClient, names_event
from pitwall.sources.http import SourceUnavailable
from pitwall.timeutil import safe_zone

log = logging.getLogger(__name__)

URL_KEY = "fia_documents_url"
LAST_POLL_KEY = "fia_last_poll"
FAST, SLOW = timedelta(minutes=30), timedelta(hours=2)
MAX_DOCS_PER_RUN = 40


# ---------------------------------------------------------------------------- schedule


def _track_tz(event: Event) -> ZoneInfo:
    return safe_zone(event.circuit.tz_name) or ZoneInfo("UTC")


def race_week_event(db: DB, now: datetime) -> Event | None:
    """The event whose race week (Wednesday to race day, track-local) contains `now`."""
    races = db.execute(
        select(Event, Session)
        .join(Session, Session.event_id == Event.id)
        .where(
            Session.type == "RACE",
            Session.start_utc > now - timedelta(days=2),
            Session.start_utc < now + timedelta(days=7),
            Event.status == "scheduled",
        )
        .order_by(Session.start_utc)
    ).all()
    for ev, race in races:
        tz = _track_tz(ev)
        race_day = race.start_utc.astimezone(tz).date()
        today = now.astimezone(tz).date()
        wednesday = race_day - timedelta(days=(race_day.weekday() - 2) % 7)
        if wednesday <= today <= race_day:
            return ev
    return None


def interval_for(event: Event, now: datetime) -> timedelta:
    local = now.astimezone(_track_tz(event))
    return FAST if local.weekday() in (4, 5) else SLOW  # Friday, Saturday


def due(db: DB, now: datetime) -> Event | None:
    """The race-week event to poll now, or None (not race week, or polled recently)."""
    event = race_week_event(db, now)
    if event is None:
        return None
    last = get_kv(db, LAST_POLL_KEY)
    if last:
        try:
            elapsed = now - datetime.fromisoformat(last)
        except ValueError:
            elapsed = None
        if elapsed is not None and elapsed < interval_for(event, now) - timedelta(minutes=2):
            return None
    return event


# ---------------------------------------------------------------------------- changes


@dataclass
class Changes:
    """What a poll (or a manual entry) added, for alerts. Plain values only, so the result can
    outlive the database session that produced it."""

    event_id: int
    grid: list[int] = field(default_factory=list)  # GridPenalty ids
    licence: list[tuple[str, int, int]] = field(default_factory=list)  # driver, before, after
    usage: list[tuple[int, int | None]] = field(default_factory=list)  # PuUsage id, previous

    def __bool__(self) -> bool:
        return bool(self.grid or self.licence or self.usage)


def licence_crossings(
    db: DB, driver_ids: set[str], before: dict[str, int], today: date | None = None
) -> list[tuple[str, int, int]]:
    out = []
    for d in sorted(driver_ids):
        after = penalties.driver_total(db, d, today)
        if after > before.get(d, 0) and after >= penalties.WARN_POINTS[0]:
            out.append((d, before.get(d, 0), after))
    return out


def poll(client: FiaClient, now: datetime | None = None, *, force: bool = False) -> Changes | None:
    """Fetch new documents for the race-week event. Returns what changed (None if nothing
    was due or the source isn't configured)."""
    now = now or utcnow()
    with session_scope() as db:
        url = get_kv(db, URL_KEY)
        event = race_week_event(db, now) if force else due(db, now)
        if not url or event is None:
            return None
        set_kv(db, LAST_POLL_KEY, now.isoformat())
        event_id, name, season = event.id, event.name, event.season
        known = set(db.scalars(select(FiaDocument.url)))
    try:
        refs = [r for r in client.listing(url) if names_event(r, name, season)]
    except SourceUnavailable as exc:
        log.warning("FIA documents page unavailable", extra={"error": str(exc)})
        return None
    changes = Changes(event_id)
    for ref in [r for r in refs if r.url not in known][:MAX_DOCS_PER_RUN]:
        try:
            pdf = client.pdf(ref.url)
        except SourceUnavailable as exc:
            log.warning("FIA document unavailable", extra={"url": ref.url, "error": str(exc)})
            continue
        with session_scope() as db:
            event = db.get(Event, event_id)
            if event is None:
                return changes
            before = {p.driver.id: p.total for p in penalties.licence_table(db, now.date())}
            prev = penalties.previous_counts(db, event)
            res = fia_docs.ingest(
                db, event, url=ref.url, title=ref.title, pdf=pdf, published_at=ref.published_at
            )
            db.flush()
            changes.grid += [g.id for g in res.grid]
            changes.usage += [
                (u.id, prev.get((u.driver_id, u.element)))
                for u in res.usage
                if u.count > prev.get((u.driver_id, u.element), 0)
            ]
            changes.licence += licence_crossings(
                db, {p.driver_id for p in res.points}, before, now.date()
            )
    return changes


# ---------------------------------------------------------------------------- alerts


def _published(db: DB, document_id: int | None, fallback: datetime) -> datetime | None:
    doc = db.get(FiaDocument, document_id) if document_id else None
    return doc.published_at if doc and doc.published_at else fallback


def _spoiler(event: Event, published: datetime | None) -> bool:
    """Published during/after the event's first competitive session = post-session news."""
    start = spoilers.first_competitive_start(event)
    return start is not None and (published is None or published >= start)


def _source_line(db: DB, document_id: int | None) -> str:
    doc = db.get(FiaDocument, document_id) if document_id else None
    return f"Source: {doc.title} — {doc.url}" if doc else "Entered by a Pitwall admin."


def grid_message(db: DB, penalty_id: int, user_id: int) -> Message | None:
    p = db.get(GridPenalty, penalty_id)
    if p is None or not (p.places or p.back_of_grid):
        return None
    ev = db.get(Event, p.event_id)
    if ev is None:
        return None
    what = penalties.describe_grid(p)
    lines = [f"{p.driver.full_name}: {what} at the {ev.name}."]
    if p.reason:
        lines.append(f"Reason: {p.reason}")
    lines += [_source_line(db, p.document_id), event_url(ev)]
    return Message(
        user_id=user_id,
        type="penalty",
        title=f"⚠️ Grid penalty: {p.driver.full_name} ({what}), {ev.short_name}",
        text="\n".join(lines),
        event_id=ev.id,
        url=event_url(ev),
        data={
            "driver": p.driver_id,
            "places": p.places,
            "back_of_grid": p.back_of_grid,
            "reason": p.reason,
        },
        dedup_content=f"grid:{ev.id}:{p.driver_id}:{p.places}:{p.back_of_grid}:{p.reason}",
        spoiler=_spoiler(ev, _published(db, p.document_id, p.recorded_at)),
    )


def licence_message(
    db: DB, event: Event, driver_id: str, after: int, user_id: int, published: datetime | None
) -> Message | None:
    driver = db.get(Driver, driver_id)
    if driver is None:
        return None
    table = {p.driver.id: p for p in penalties.licence_table(db)}
    entry = table.get(driver_id)
    if after >= penalties.BAN_POINTS:
        title = f"🟥 {driver.full_name} has {after} licence points: a race ban"
        lead = "12 penalty points within 12 months means a one-race ban."
    else:
        title = f"🟨 {driver.full_name} now has {after} licence points"
        lead = f"{penalties.BAN_POINTS - after} more within 12 months would mean a race ban."
    lines = [lead]
    if entry and entry.next_expiry:
        e = entry.next_expiry
        lines.append(f"Next to expire: {e.row.points} on {e.expires:%d %b %Y}.")
    return Message(
        user_id=user_id,
        type="licence",
        title=title,
        text="\n".join(lines),
        event_id=event.id,
        url=event_url(event),
        data={"driver": driver_id, "points": after},
        dedup_content=f"licence:{driver_id}:{after}:{event.id}",
        spoiler=_spoiler(event, published),
    )


def usage_message(
    db: DB, usage_id: int, previous: int | None, user_id: int, followed: set[str]
) -> Message | None:
    u = db.get(PuUsage, usage_id)
    if u is None or u.driver_id not in followed:
        return None
    ev = db.get(Event, u.event_id)
    if ev is None:
        return None
    alloc = penalties.allocations(db, ev.season).get(u.element)
    of = f" of {alloc} allowed" if alloc else ""
    over = " (over the allocation: expect a grid penalty)" if alloc and u.count > alloc else ""
    return Message(
        user_id=user_id,
        type="pu_element",
        title=f"🔧 {u.driver.full_name} takes a new {u.element}: number {u.count}{of}",
        text="\n".join(
            [
                f"{u.driver.full_name} is using {u.element} number {u.count}{of} at the "
                f"{ev.name}{over}.",
                _source_line(db, u.document_id),
                event_url(ev),
            ]
        ),
        event_id=ev.id,
        url=event_url(ev),
        data={"driver": u.driver_id, "element": u.element, "count": u.count, "allowed": alloc},
        dedup_content=f"pu:{ev.id}:{u.driver_id}:{u.element}:{u.count}",
        spoiler=_spoiler(ev, _published(db, u.document_id, u.recorded_at)),
    )


FanOut = Callable[[DB, Callable[[AppSettings, int], Message | None], datetime], int]


def send_alerts(changes: Changes, fan_out: FanOut, now: datetime | None = None) -> int:
    """Queue alerts for every user. Returns how many were queued."""
    now = now or utcnow()
    queued = 0
    with session_scope() as db:
        event = db.get(Event, changes.event_id)
        if event is None:
            return 0
        followed_by: dict[int, set[str]] = {}
        for f in db.scalars(select(FollowedDriver)):
            followed_by.setdefault(f.user_id, set()).add(f.driver_id)
        for gid in changes.grid:
            queued += fan_out(db, lambda _s, uid, gid=gid: grid_message(db, gid, uid), now)
        for driver_id, _before, after in changes.licence:
            queued += fan_out(
                db,
                lambda _s, uid, d=driver_id, a=after: licence_message(db, event, d, a, uid, now),
                now,
            )
        for uid_usage, prev in changes.usage:
            queued += fan_out(
                db,
                lambda _s, uid, u=uid_usage, p=prev: usage_message(
                    db, u, p, uid, followed_by.get(uid, set())
                ),
                now,
            )
    return queued
