"""View models shared by the web UI, notifications and the ICS feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env
from pitwall.db import utcnow
from pitwall.models import SESSION_EMOJI, SESSION_LABELS, Event, Session
from pitwall.settings_store import AppSettings
from pitwall.timeutil import fmt_local, fmt_tz_abbr, is_odd_hour, safe_zone


@dataclass
class SessionView:
    id: int
    type: str
    label: str
    emoji: str
    start_utc: datetime | None
    end_utc: datetime | None
    owner_time: str
    owner_tz: str
    local_time: str
    local_tz: str
    odd: bool
    status: str
    is_past: bool

    @property
    def owner_clock(self) -> str:
        return self.owner_time.split(", ")[-1] if self.owner_time else ""


def session_view(sess: Session, settings: AppSettings, now: datetime | None = None) -> SessionView:
    now = now or utcnow()
    tz = settings.tz
    track_tz = safe_zone(sess.event.circuit.tz_name) if sess.event and sess.event.circuit else None
    return SessionView(
        id=sess.id,
        type=sess.type,
        label=SESSION_LABELS.get(sess.type, sess.type),
        emoji=SESSION_EMOJI.get(sess.type, "🏎️"),
        start_utc=sess.start_utc,
        end_utc=sess.end_utc_est,
        owner_time=fmt_local(sess.start_utc, tz),
        owner_tz=fmt_tz_abbr(sess.start_utc, tz),
        local_time=fmt_local(sess.start_utc, track_tz) if track_tz else "",
        local_tz=fmt_tz_abbr(sess.start_utc, track_tz) if track_tz else "",
        odd=sess.status != "cancelled" and is_odd_hour(sess.start_utc, tz, settings.odd_window),
        status=sess.status,
        is_past=bool(sess.end_utc_est and sess.end_utc_est < now),
    )


def event_sessions(
    event: Event, settings: AppSettings, now: datetime | None = None
) -> list[SessionView]:
    order = {t: i for i, t in enumerate(("FP1", "FP2", "FP3", "SQ", "SPRINT", "Q", "RACE"))}
    sessions = sorted(
        event.sessions,
        key=lambda s: (
            s.start_utc is None,
            s.start_utc.timestamp() if s.start_utc else 0,
            order.get(s.type, 9),
        ),
    )
    return [session_view(s, settings, now) for s in sessions]


def odd_hour_note(view: SessionView) -> str | None:
    if not view.odd or view.start_utc is None:
        return None
    return f"{view.label} starts at {view.owner_clock} your time"


def event_url(event: Event) -> str:
    return f"{get_env().base_url.rstrip('/')}/events/{event.id}"


def next_session(db: DB, now: datetime | None = None) -> Session | None:
    now = now or utcnow()
    return db.scalars(
        select(Session)
        .join(Event)
        .where(Session.start_utc > now, Session.status == "scheduled", Event.status == "scheduled")
        .order_by(Session.start_utc)
        .limit(1)
    ).first()


def current_or_next_event(db: DB, now: datetime | None = None) -> Event | None:
    """The event whose race hasn't finished yet (this weekend's, or the next one)."""
    now = now or utcnow()
    return db.scalars(
        select(Event)
        .join(Session)
        .where(
            Session.type == "RACE",
            Session.end_utc_est > now,
            Event.status == "scheduled",
            Session.status == "scheduled",
        )
        .order_by(Session.start_utc)
        .limit(1)
    ).first()


def last_completed_event(db: DB, now: datetime | None = None) -> Event | None:
    now = now or utcnow()
    return db.scalars(
        select(Event)
        .join(Session)
        .where(Session.type == "RACE", Session.end_utc_est <= now, Event.status == "scheduled")
        .order_by(Session.start_utc.desc())
        .limit(1)
    ).first()
