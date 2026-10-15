"""ICS calendar feed. UIDs are derived from the event slug + session type, so they are stable
across syncs, restarts and round renumbering; SEQUENCE increments whenever a session changes."""

from __future__ import annotations

from datetime import datetime, timedelta

from icalendar import Calendar
from icalendar import Event as ICalEvent
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall import __version__
from pitwall.db import utcnow
from pitwall.models import SESSION_EMOJI, SESSION_LABELS, Event, Session
from pitwall.services.presenters import event_url
from pitwall.settings_store import AppSettings
from pitwall.timeutil import fmt_local, fmt_tz_abbr, safe_zone

SESSION_ALIASES: dict[str, set[str]] = {
    "fp1": {"FP1"},
    "fp2": {"FP2"},
    "fp3": {"FP3"},
    "practice": {"FP1", "FP2", "FP3"},
    "fp": {"FP1", "FP2", "FP3"},
    "sq": {"SQ"},
    "sprintquali": {"SQ"},
    "sprint_qualifying": {"SQ"},
    "sprint": {"SPRINT"},
    "q": {"Q"},
    "quali": {"Q"},
    "qualifying": {"Q"},
    "race": {"RACE"},
    "r": {"RACE"},
}


def parse_filter(raw: str | None) -> set[str] | None:
    """'race,q' -> {'RACE', 'Q'}. None/empty -> None (all sessions). Unknown names are ignored."""
    if not raw:
        return None
    out: set[str] = set()
    for part in raw.split(","):
        out |= SESSION_ALIASES.get(part.strip().lower(), set())
    return out or None


def session_uid(event: Event, sess: Session) -> str:
    return f"{event.slug}-{sess.type.lower()}@pitwall"


def session_title(event: Event, sess: Session) -> str:
    emoji = SESSION_EMOJI.get(sess.type, "🏎️")
    return f"{emoji} {SESSION_LABELS.get(sess.type, sess.type)} — {event.short_name}"


def build_calendar(
    db: DB, settings: AppSettings, types: set[str] | None = None, now: datetime | None = None
) -> bytes:
    now = now or utcnow()
    cal = Calendar()
    cal.add("prodid", f"-//Pitwall//F1 {__version__}//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Pitwall F1")
    cal.add("x-wr-caldesc", "Formula 1 sessions from Pitwall")
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", timedelta(hours=6), parameters={"VALUE": "DURATION"})

    q = (
        select(Session)
        .join(Event)
        .where(Event.season >= now.year - 1, Session.start_utc.is_not(None))
        .order_by(Session.start_utc)
    )
    for sess in db.scalars(q):
        if types is not None and sess.type not in types:
            continue
        event = sess.event
        circuit = event.circuit
        track_tz = safe_zone(circuit.tz_name)
        ve = ICalEvent()
        ve.add("uid", session_uid(event, sess))
        ve.add("summary", session_title(event, sess))
        ve.add("dtstart", sess.start_utc)
        ve.add("dtend", sess.end_utc_est or sess.start_utc)
        ve.add("dtstamp", sess.last_changed_at)
        ve.add("last-modified", sess.last_changed_at)
        ve.add("sequence", sess.sequence)
        ve.add("location", circuit.name)
        lines = []
        if track_tz is not None:
            lines.append(
                f"Local time: {fmt_local(sess.start_utc, track_tz)} "
                f"{fmt_tz_abbr(sess.start_utc, track_tz)}"
                + (f" ({circuit.locality})" if circuit.locality else "")
            )
        lines.append(
            f"Your time: {fmt_local(sess.start_utc, settings.tz)} "
            f"{fmt_tz_abbr(sess.start_utc, settings.tz)}"
        )
        lines.append(event_url(event))
        ve.add("description", "\n".join(lines))
        ve.add("url", event_url(event))
        cancelled = sess.status == "cancelled" or event.status == "cancelled"
        ve.add("status", "CANCELLED" if cancelled else "CONFIRMED")
        ve.add("transp", "TRANSPARENT")
        cal.add_component(ve)
    return cal.to_ical()
