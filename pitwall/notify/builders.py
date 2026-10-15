"""Builds alert messages (plain text + HTML email + webhook data) from DB state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import (
    OWNER_ID,
    SESSION_EMOJI,
    SESSION_LABELS,
    Event,
    FollowedDriver,
    Result,
    Session,
)
from pitwall.notify.message import Message
from pitwall.services import circuit_maps, title_math, track_stats, trivia
from pitwall.services import standings as standings_svc
from pitwall.services import weather as weather_svc
from pitwall.services.presenters import (
    SessionView,
    event_sessions,
    event_url,
    odd_hour_note,
    session_view,
)
from pitwall.services.standings import fmt_points
from pitwall.services.sync import EventChange
from pitwall.settings_store import AppSettings
from pitwall.templating import render
from pitwall.timeutil import fmt_local, fmt_tz_abbr, humanize_delta, offset_label


def _place(event: Event) -> str:
    c = event.circuit
    bits = [c.name, c.locality, c.country]
    return ", ".join(b for b in bits if b)


# ---------------------------------------------------------------------------- reminders


def reminder(
    sess: Session, offset_minutes: int, settings: AppSettings, now: datetime | None = None
) -> Message:
    event = sess.event
    view = session_view(sess, settings, now)
    label = SESSION_LABELS.get(sess.type, sess.type)
    emoji = SESSION_EMOJI.get(sess.type, "🏎️")
    title = f"{emoji} {label} in {offset_label(offset_minutes)} — {event.short_name}"
    lines = [f"{label} — {event.name}", f"Your time: {view.owner_time} {view.owner_tz}"]
    if view.local_time:
        lines.append(f"Track time: {view.local_time} {view.local_tz}")
    lines.append(f"Where: {_place(event)}")
    note = odd_hour_note(view)
    if note:
        lines.append(f"⚠️ {note}")
    lines.append(event_url(event))
    text = "\n".join(lines)
    html = render("email/simple.html", title=title, lines=lines[:-1], link=event_url(event))
    start = sess.start_utc.isoformat() if sess.start_utc else ""
    return Message(
        type="reminder",
        title=title,
        text=text,
        html=html,
        event_id=event.id,
        session_id=sess.id,
        url=event_url(event),
        expires_at=sess.start_utc,
        data={
            "session": sess.type,
            "start_utc": start,
            "offset_minutes": offset_minutes,
            "event": event.name,
            "circuit": event.circuit.name,
            "odd_hours": view.odd,
        },
        dedup_content=f"reminder:{sess.id}:{offset_minutes}:{start}",
    )


# ---------------------------------------------------------------------------- schedule change


def _fmt_owner(dt: datetime | None, settings: AppSettings) -> str:
    if dt is None:
        return "TBC"
    return f"{fmt_local(dt, settings.tz)} {fmt_tz_abbr(dt, settings.tz)}"


def schedule_change(db: DB, change: EventChange, settings: AppSettings) -> Message:
    event = db.get(Event, change.event_id)
    assert event is not None
    if change.kind == "cancelled":
        title = f"❌ Cancelled — {event.name}"
        head = f"{event.name} has been removed from the calendar."
    elif change.kind == "added":
        title = f"🆕 Added to the calendar — {event.name}"
        head = f"{event.name} is on the calendar."
    else:
        title = f"🔄 Schedule change — {event.name}"
        head = f"Session times changed for the {event.name}."
    lines = [head]
    for sc in change.sessions:
        label = SESSION_LABELS.get(sc.session_type, sc.session_type)
        if sc.kind == "moved":
            lines.append(
                f"• {label}: moved from {_fmt_owner(sc.old_start, settings)} "
                f"to {_fmt_owner(sc.new_start, settings)}"
            )
        elif sc.kind == "confirmed":
            lines.append(f"• {label}: time confirmed — {_fmt_owner(sc.new_start, settings)}")
        elif sc.kind == "added":
            lines.append(f"• {label}: added — {_fmt_owner(sc.new_start, settings)}")
        elif sc.kind == "cancelled":
            lines.append(f"• {label}: cancelled (was {_fmt_owner(sc.old_start, settings)})")
    if change.kind == "added":
        for v in event_sessions(event, settings):
            lines.append(f"• {v.label}: {v.owner_time} {v.owner_tz}")
    lines.append("Reminders and the calendar feed have been updated.")
    text = "\n".join([*lines, event_url(event)])
    html = render("email/simple.html", title=title, lines=lines, link=event_url(event))
    return Message(
        type="schedule_change",
        title=title,
        text=text,
        html=html,
        event_id=event.id,
        url=event_url(event),
        data={
            "event": event.name,
            "kind": change.kind,
            "sessions": [
                {
                    "session": sc.session_type,
                    "change": sc.kind,
                    "old_start_utc": sc.old_start.isoformat() if sc.old_start else None,
                    "new_start_utc": sc.new_start.isoformat() if sc.new_start else None,
                }
                for sc in change.sessions
            ],
        },
    )


# ---------------------------------------------------------------------------- results


def _followed_ids(db: DB, user_id: int = OWNER_ID) -> set[str]:
    return {
        f.driver_id
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user_id))
    }


def results(
    db: DB, sess: Session, settings: AppSettings, user_id: int = OWNER_ID
) -> Message | None:
    rows = list(
        db.scalars(
            select(Result)
            .where(Result.session_id == sess.id)
            .order_by(Result.position.is_(None), Result.position)
        )
    )
    if not rows:
        return None
    event = sess.event
    label = SESSION_LABELS.get(sess.type, sess.type)
    title = f"{SESSION_EMOJI.get(sess.type, '🏁')} {label} results — {event.short_name}"
    followed = _followed_ids(db, user_id)
    top_n = 10

    def line(r: Result) -> str:
        pos = (
            r.position_text
            if r.position is None or (r.position_text or "").isalpha()
            else f"P{r.position}"
        )
        team = f" ({r.constructor.name})" if r.constructor else ""
        if sess.type == "Q":
            best = r.q3 or r.q2 or r.q1 or ""
            extra = f" — {best}" if best else ""
        else:
            extra = f" — {r.time_text or r.status or ''}".rstrip(" —")
            if r.points:
                extra += f" (+{fmt_points(r.points)} pts)"
            if r.fastest_lap_rank == 1:
                extra += " ⏱️ fastest lap"
        star = "★ " if r.driver_id in followed else ""
        return f"{star}{pos} {r.driver.full_name}{team}{extra}"

    lines = [line(r) for r in rows[:top_n]]
    extra_followed = [line(r) for r in rows[top_n:] if r.driver_id in followed]
    if extra_followed:
        lines.append("Followed drivers:")
        lines.extend(extra_followed)
    text = "\n".join([f"{label} — {event.name}", *lines, event_url(event)])
    html = render("email/simple.html", title=title, lines=lines, link=event_url(event))
    return Message(
        user_id=user_id,
        type="results",
        title=title,
        text=text,
        html=html,
        event_id=event.id,
        session_id=sess.id,
        url=event_url(event),
        data={
            "event": event.name,
            "session": sess.type,
            "results": [
                {
                    "position": r.position,
                    "driver": r.driver.full_name,
                    "constructor": r.constructor.name if r.constructor else None,
                    "points": r.points,
                    "status": r.status,
                }
                for r in rows[:top_n]
            ],
        },
    )


# ---------------------------------------------------------------------------- standings


def standings(db: DB, season: int, event: Event | None, user_id: int = OWNER_ID) -> Message | None:
    drivers = standings_svc.table(db, season, "driver", user_id=user_id)
    constructors = standings_svc.table(db, season, "constructor", user_id=user_id)
    if drivers is None:
        return None
    after = f" after {event.short_name}" if event else f" after round {drivers.round}"
    title = f"🏆 Championship standings{after}"

    def row_line(r: standings_svc.StandingRow) -> str:
        gap = f" (-{fmt_points(r.gap_leader)})" if r.gap_leader else ""
        star = "★ " if r.followed else ""
        return f"{star}{r.position}. {r.name} — {fmt_points(r.points)} pts{gap} {r.arrow}"

    lines = ["Drivers:"] + [row_line(r) for r in drivers.rows[:10]]
    lines += [row_line(r) for r in drivers.rows[10:] if r.followed]
    if constructors:
        lines += ["Constructors:"] + [row_line(r) for r in constructors.rows[:5]]
    text = "\n".join([*lines, event_url(event) if event else ""]).strip()
    html = render(
        "email/standings.html",
        title=title,
        drivers=drivers,
        constructors=constructors,
        link=event_url(event) if event else None,
    )
    content = "|".join(f"{r.entity_id}:{r.points}" for r in drivers.rows)
    if constructors:
        content += "|" + "|".join(f"{r.entity_id}:{r.points}" for r in constructors.rows)
    return Message(
        user_id=user_id,
        type="standings",
        title=title,
        text=text,
        html=html,
        event_id=event.id if event else None,
        url=event_url(event) if event else None,
        data={
            "season": season,
            "round": drivers.round,
            "drivers": [
                {"position": r.position, "name": r.name, "points": r.points, "change": r.change}
                for r in drivers.rows[:10]
            ],
        },
        dedup_content=f"standings:{season}:{drivers.round}:{content}",
    )


# ---------------------------------------------------------------------------- preview


@dataclass
class FormLine:
    driver: str
    results: list[tuple[str, str]] = field(default_factory=list)  # (event short name, "P3")


@dataclass
class PreviewContext:
    event: Event
    sessions: list[SessionView]
    odd_notes: list[str]
    stats: track_stats.TrackStats
    drivers: standings_svc.StandingsTable | None
    constructors: standings_svc.StandingsTable | None
    followed_rows: list[standings_svc.StandingRow]
    form: list[FormLine]
    link: str
    countdown: str | None
    title: list[title_math.TitleMath] = field(default_factory=list)
    facts: list[trivia.Fact] = field(default_factory=list)
    weather: dict[int, weather_svc.WeatherView] = field(default_factory=dict)
    rain: list[str] = field(default_factory=list)
    map_url: str | None = None


def _form(
    db: DB, settings: AppSettings, before: datetime, user_id: int = OWNER_ID
) -> list[FormLine]:
    out: list[FormLine] = []
    for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user_id)):
        rows = db.execute(
            select(Result, Event)
            .join(Session, Result.session_id == Session.id)
            .join(Event, Session.event_id == Event.id)
            .where(
                Result.driver_id == f.driver_id, Session.type == "RACE", Session.start_utc < before
            )
            .order_by(Session.start_utc.desc())
            .limit(3)
        ).all()
        line = FormLine(f.driver.full_name)
        for res, ev in rows:
            pos = (
                f"P{res.position}"
                if res.position_text and res.position_text.isdigit()
                else ("DNF" if res.position_text == "R" else (res.position_text or "?"))
            )
            label = ev.short_name if ev.season == before.year else f"{ev.short_name} {ev.season}"
            line.results.append((label, pos))
        out.append(line)
    return out


def preview_title_math(db: DB, event: Event) -> list[title_math.TitleMath]:
    out = []
    for kind in ("driver", "constructor"):
        tm = title_math.for_season(db, event.season, kind)
        # Only when the standings lead into this event (or the title is decided) and it matters.
        if tm and tm.relevant and (tm.champion or tm.next_event == event.name):
            out.append(tm)
    return out


def preview_context(
    db: DB,
    event: Event,
    settings: AppSettings,
    now: datetime | None = None,
    *,
    avoid_recent: bool = True,
    user_id: int = OWNER_ID,
) -> PreviewContext:
    now = now or utcnow()
    sessions = event_sessions(event, settings, now)
    notes = [n for v in sessions if (n := odd_hour_note(v))]
    season = standings_svc.latest_season_with_standings(db)
    drivers = constructors = None
    if season is not None and season >= event.season - 1:
        drivers = standings_svc.table(db, season, "driver", user_id=user_id)
        constructors = standings_svc.table(db, season, "constructor", user_id=user_id)
    race = next((s for s in event.sessions if s.type == "RACE"), None)
    first = min((s.start_utc for s in event.sessions if s.start_utc), default=None)
    wx = weather_svc.for_event(db, event)
    return PreviewContext(
        event=event,
        sessions=sessions,
        odd_notes=notes,
        stats=track_stats.compute(db, event),
        drivers=drivers,
        constructors=constructors,
        followed_rows=[r for r in (drivers.rows if drivers else []) if r.followed],
        form=_form(db, settings, race.start_utc if race and race.start_utc else now, user_id),
        link=event_url(event),
        countdown=humanize_delta((first - now).total_seconds()) if first and first > now else None,
        title=preview_title_math(db, event),
        facts=trivia.for_preview(db, event, avoid_recent=avoid_recent, user_id=user_id),
        weather=wx,
        rain=weather_svc.rain_warnings(event, wx),
        map_url=circuit_maps.map_url(event.circuit),
    )


def preview(
    db: DB,
    event: Event,
    settings: AppSettings,
    now: datetime | None = None,
    *,
    avoid_recent: bool = True,
    user_id: int = OWNER_ID,
) -> Message:
    """`data["facts"]` lists the trivia used, so the caller can remember them (no repeats)."""
    ctx = preview_context(db, event, settings, now, avoid_recent=avoid_recent, user_id=user_id)
    badge = " ⚡ Sprint weekend" if event.is_sprint_weekend else ""
    title = f"🏎️ Race week: {event.name}{badge}"
    text = render("email/preview.txt", ctx=ctx, title=title).strip()
    html = render("email/preview.html", ctx=ctx, title=title)
    data: dict[str, Any] = {
        "event": event.name,
        "round": event.round,
        "season": event.season,
        "circuit": event.circuit.name,
        "locality": event.circuit.locality,
        "country": event.circuit.country,
        "sprint_weekend": event.is_sprint_weekend,
        "sessions": [
            {
                "session": v.type,
                "start_utc": v.start_utc.isoformat() if v.start_utc else None,
                "odd_hours": v.odd,
            }
            for v in ctx.sessions
        ],
        "title_math": [{"kind": tm.kind, "lines": tm.summary()} for tm in ctx.title],
        "rain_risk": ctx.rain,
        "map_url": ctx.map_url,
        "facts": [{"type": f.type, "text": f.text, "key": f.key} for f in ctx.facts],
    }
    return Message(
        user_id=user_id,
        type="preview",
        title=title,
        text=text,
        html=html,
        event_id=event.id,
        url=ctx.link,
        data=data,
        dedup_content=f"preview:{event.slug}",
    )


def test_message(channel: str) -> Message:
    now = utcnow()
    title = "✅ Pitwall test notification"
    text = f"This is a test of the {channel} channel, sent {now:%Y-%m-%d %H:%M} UTC."
    html = render("email/simple.html", title=title, lines=[text], link=None)
    return Message(
        type="test",
        title=title,
        text=text,
        html=html,
        dedup_content=f"test:{channel}:{now.isoformat()}",
        expires_at=now + timedelta(hours=1),
    )
