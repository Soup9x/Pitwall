"""Weekly digest: one summary instead of individual alerts for the chosen alert types.

While digest mode is on, the outbox holds those alerts (hold_reason="digest"). At the
configured day/time `build()` gathers them into one message, adds the week ahead (sessions,
rain risk) and the top of the standings, and marks the gathered alerts as digested."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Event, NotificationLog, Outbox, Session
from pitwall.notify.message import Message
from pitwall.services import standings as standings_svc
from pitwall.services import weather
from pitwall.services.presenters import SessionView, session_view
from pitwall.services.standings import StandingsTable
from pitwall.settings_store import ALERT_LABELS, AppSettings
from pitwall.templating import render

SECTION_ORDER = ("schedule_change", "results", "standings")


@dataclass
class DigestItem:
    title: str
    text: str


@dataclass
class DigestContext:
    title: str
    sections: list[tuple[str, list[DigestItem]]]
    upcoming: list[tuple[Event, SessionView, weather.WeatherView | None]] = field(
        default_factory=list
    )
    drivers: StandingsTable | None = None
    link: str = ""


def held(db: DB) -> list[Outbox]:
    return list(
        db.scalars(
            select(Outbox)
            .where(Outbox.status == "pending", Outbox.hold_reason == "digest")
            .order_by(Outbox.created_at, Outbox.id)
        )
    )


def build(
    db: DB, settings: AppSettings, now: datetime, base_url: str
) -> tuple[Message, list[Outbox]] | None:
    rows = held(db)
    grouped: dict[str, list[DigestItem]] = {}
    for row in rows:
        lines = row.text.splitlines()
        if lines and lines[-1].startswith(("http://", "https://")):
            lines = lines[:-1]  # the event link; the digest links to Pitwall once
        body = "\n".join(lines)
        grouped.setdefault(row.type, []).append(DigestItem(row.title, body))
    sections = [(ALERT_LABELS.get(t, t), grouped[t]) for t in SECTION_ORDER if t in grouped]
    sections += [
        (ALERT_LABELS.get(t, t), items) for t, items in grouped.items() if t not in SECTION_ORDER
    ]

    week = db.scalars(
        select(Session)
        .join(Event)
        .where(
            Session.start_utc >= now,
            Session.start_utc < now + timedelta(days=7),
            Session.status == "scheduled",
            Event.status == "scheduled",
        )
        .order_by(Session.start_utc)
    )
    upcoming = []
    for sess in week:
        wx = weather.for_event(db, sess.event).get(sess.id)
        upcoming.append((sess.event, session_view(sess, settings, now), wx))
    season = standings_svc.latest_season_with_standings(db)
    drivers = standings_svc.table(db, season, "driver") if season else None
    if not rows and not upcoming:
        return None

    local = now.astimezone(settings.tz)
    title = f"🗞️ Your F1 week — {local:%a %d %b}"
    ctx = DigestContext(title, sections, upcoming, drivers, base_url.rstrip("/"))
    text = render("email/digest.txt", ctx=ctx).strip()
    html = render("email/digest.html", ctx=ctx, title=title, link=ctx.link)
    msg = Message(
        type="digest",
        title=title,
        text=text,
        html=html,
        url=ctx.link,
        data={
            "items": [{"type": r.type, "title": r.title} for r in rows],
            "upcoming_sessions": len(upcoming),
        },
        dedup_content=f"digest:{local:%Y-%m-%d}",
    )
    return msg, rows


def mark_digested(db: DB, rows: list[Outbox], now: datetime) -> None:
    for row in rows:
        row.status, row.hold_reason, row.resolved_at = "digested", None, now
        db.add(
            NotificationLog(
                type=row.type,
                channel="digest",
                payload_hash=row.payload_hash,
                title=row.title,
                event_id=row.event_id,
                session_id=row.session_id,
                sent_at=now,
                status="digested",
                detail="included in the weekly digest",
            )
        )
    db.flush()
