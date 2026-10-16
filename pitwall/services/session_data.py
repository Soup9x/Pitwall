"""OpenF1 post-session data for every session from 2023: race control messages and measured
conditions (track/air temperature, humidity, rain, wind).

Fetched once a session has settled, then never again (completed sessions don't change). Values
that are missing or physically implausible are dropped, never filled in: a summary built from
nothing is stored as empty and the UI hides it."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import session_scope, utcnow
from pitwall.models import (
    SESSION_LABELS,
    Event,
    RaceControlMessage,
    Session,
    SessionConditions,
    SessionDataFetch,
)
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.openf1 import ALL_SESSION_NAMES, FIRST_SEASON, OpenF1Client

log = logging.getLogger(__name__)

SETTLE = timedelta(hours=1)  # ask only once the session has been over for a while
GIVE_UP = timedelta(days=2)  # after this, an empty answer is final
SERIES_POINTS = 90

# Plausible ranges; anything outside is a sensor glitch and is ignored.
_RANGES = {
    "air_temperature": (-20.0, 60.0),
    "track_temperature": (-20.0, 80.0),
    "humidity": (0.0, 100.0),
    "wind_speed": (0.0, 60.0),
}


def _dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _num(row: dict[str, Any], key: str) -> float | None:
    try:
        v = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    lo, hi = _RANGES.get(key, (float("-inf"), float("inf")))
    return v if lo <= v <= hi else None


# ---------------------------------------------------------------------------- parsing


def parse_race_control(rows: Iterable[dict[str, Any]], session_id: int) -> list[RaceControlMessage]:
    out: dict[tuple[datetime, str], RaceControlMessage] = {}
    for r in rows:
        at, msg = _dt(r.get("date")), (r.get("message") or "").strip()
        if at is None or not msg:
            continue
        out[(at, msg)] = RaceControlMessage(
            session_id=session_id,
            at=at,
            lap=_int(r.get("lap_number")),
            category=(r.get("category") or None),
            flag=(r.get("flag") or None),
            scope=(r.get("scope") or None),
            sector=_int(r.get("sector")),
            driver_number=_int(r.get("driver_number")),
            message=msg[:2000],
        )
    return sorted(out.values(), key=lambda m: m.at)


def _stats(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    return round(min(values), 1), round(max(values), 1), round(sum(values) / len(values), 1)


def summarize_weather(
    rows: list[dict[str, Any]], session_id: int, start: datetime | None
) -> SessionConditions:
    samples = [r for r in rows if _dt(r.get("date")) is not None]
    track = [v for r in samples if (v := _num(r, "track_temperature")) is not None]
    air = [v for r in samples if (v := _num(r, "air_temperature")) is not None]
    hum = [v for r in samples if (v := _num(r, "humidity")) is not None]
    wind = [v for r in samples if (v := _num(r, "wind_speed")) is not None]
    rain_vals = [r["rainfall"] for r in samples if isinstance(r.get("rainfall"), (int, float))]
    t_lo, t_hi, t_avg = _stats(track)
    a_lo, a_hi, a_avg = _stats(air)
    h_lo, h_hi, h_avg = _stats(hum)
    return SessionConditions(
        session_id=session_id,
        samples=len(samples),
        track_min=t_lo,
        track_max=t_hi,
        track_avg=t_avg,
        air_min=a_lo,
        air_max=a_hi,
        air_avg=a_avg,
        humidity_min=h_lo,
        humidity_max=h_hi,
        humidity_avg=h_avg,
        rain=any(v > 0 for v in rain_vals) if rain_vals else None,
        wind_max_ms=round(max(wind), 1) if wind else None,
        series_json=json.dumps(track_series(samples, start)),
    )


def track_series(samples: list[dict[str, Any]], start: datetime | None) -> list[list[float]]:
    """[[minutes since session start, track °C], ...], averaged into at most SERIES_POINTS."""
    pts = []
    for r in samples:
        at, v = _dt(r.get("date")), _num(r, "track_temperature")
        if at is None or v is None:
            continue
        origin = start or at
        pts.append(((at - origin).total_seconds() / 60, v))
    pts.sort()
    if not pts:
        return []
    size = max(1, -(-len(pts) // SERIES_POINTS))  # ceil
    out = []
    for i in range(0, len(pts), size):
        chunk = pts[i : i + size]
        out.append(
            [
                round(sum(p[0] for p in chunk) / len(chunk), 1),
                round(sum(p[1] for p in chunk) / len(chunk), 1),
            ]
        )
    return out


# ---------------------------------------------------------------------------- fetching


@dataclass(frozen=True)
class Target:
    session_id: int
    season: int
    stype: str
    start_utc: datetime
    end_utc: datetime


def pending(now: datetime, limit: int = 40) -> list[Target]:
    with session_scope() as db:
        done = set(db.scalars(select(SessionDataFetch.session_id)))
        rows = db.execute(
            select(Session, Event)
            .join(Event)
            .where(
                Event.season >= FIRST_SEASON,
                Event.status == "scheduled",
                Session.status == "scheduled",
                Session.start_utc.is_not(None),
                Session.end_utc_est.is_not(None),
                Session.end_utc_est < now - SETTLE,
            )
            .order_by(Session.start_utc.desc())
        ).all()
        out = [
            Target(s.id, e.season, s.type, s.start_utc, s.end_utc_est)
            for s, e in rows
            if s.id not in done and s.type in ALL_SESSION_NAMES
        ]
    return out[:limit]


def match(candidates: list[dict[str, Any]], stype: str, start: datetime) -> int | None:
    names = ALL_SESSION_NAMES[stype]
    best: tuple[timedelta, int] | None = None
    for c in candidates:
        if c.get("session_name") not in names:
            continue
        when, key = _dt(c.get("date_start")), _int(c.get("session_key"))
        if when is None or key is None:
            continue
        diff = abs(when - start)
        if diff <= timedelta(hours=3) and (best is None or diff < best[0]):
            best = (diff, key)
    return best[1] if best else None


def sync(client: OpenF1Client, now: datetime | None = None, limit: int = 40) -> int:
    """Fetch race control + weather for completed sessions. Returns sessions stored."""
    now = now or utcnow()
    stored = 0
    seasons: dict[int, list[dict[str, Any]]] = {}
    for t in pending(now, limit):
        try:
            if t.season not in seasons:
                seasons[t.season] = client.all_sessions(t.season)
            key = match(seasons[t.season], t.stype, t.start_utc)
            rc = client.race_control(key) if key is not None else []
            wx = client.weather(key) if key is not None else []
        except SourceUnavailable:
            log.warning("OpenF1 unavailable; race control and weather will be retried")
            break
        if not rc and not wx and now - t.end_utc < GIVE_UP:
            continue  # probably not published yet; try again on a later run
        messages = parse_race_control(rc, t.session_id)
        cond = summarize_weather(wx, t.session_id, t.start_utc) if wx else None
        with session_scope() as db:
            db.query(RaceControlMessage).filter(
                RaceControlMessage.session_id == t.session_id
            ).delete()
            db.add_all(messages)
            if cond is not None:
                db.merge(cond)
            db.merge(
                SessionDataFetch(
                    session_id=t.session_id,
                    openf1_session_key=key,
                    race_control_count=len(messages),
                    weather_samples=cond.samples if cond else 0,
                    fetched_at=now,
                )
            )
        stored += 1
    if stored:
        log.info("OpenF1 race control/weather stored", extra={"sessions": stored})
    return stored


# ---------------------------------------------------------------------------- race control stats

_SC_DEPLOYED = re.compile(r"(?<!VIRTUAL )SAFETY CAR DEPLOYED")
_SC_IN = re.compile(r"(?<!VIRTUAL )SAFETY CAR IN THIS LAP")
_VSC_DEPLOYED = re.compile(r"VIRTUAL SAFETY CAR DEPLOYED")
_VSC_END = re.compile(r"VIRTUAL SAFETY CAR ENDING")
_INVESTIGATION = re.compile(r"UNDER INVESTIGATION")
_PENALTY = re.compile(
    r"(\d+ SECOND TIME PENALTY|DRIVE[ -]THROUGH PENALTY|STOP[ /-]?(AND[ -])?GO PENALTY"
    r"|GRID (PLACE )?PENALTY)"
)


@dataclass
class RaceControlStats:
    safety_cars: int = 0
    vscs: int = 0
    red_flags: int = 0
    sc_laps: int | None = 0  # None when a period's laps can't be worked out
    vsc_laps: int | None = 0
    investigations: int = 0
    penalties: int = 0


def _period_laps(
    msgs: list[RaceControlMessage], start_re: re.Pattern, end_re: re.Pattern
) -> tuple[int, int | None]:
    """(number of periods, laps run during them or None if any period's laps are unknown)."""
    periods, laps, open_lap = 0, 0, None
    known = True
    for m in msgs:
        text = m.message.upper()
        if start_re.search(text):
            periods += 1
            if open_lap is not None:  # a new period without an end: can't count the old one
                known = False
            open_lap = m.lap if m.lap is not None else -1
        elif end_re.search(text) and open_lap is not None:
            if open_lap < 0 or m.lap is None or m.lap < open_lap:
                known = False
            else:
                laps += m.lap - open_lap + 1
            open_lap = None
    if open_lap is not None:  # still out when the messages stop
        known = False
    return periods, (laps if known else None)


def stats(msgs: list[RaceControlMessage]) -> RaceControlStats:
    msgs = sorted(msgs, key=lambda m: m.at)
    s = RaceControlStats()
    s.safety_cars, s.sc_laps = _period_laps(msgs, _SC_DEPLOYED, _SC_IN)
    s.vscs, s.vsc_laps = _period_laps(msgs, _VSC_DEPLOYED, _VSC_END)
    s.red_flags = sum(1 for m in msgs if (m.flag or "").upper() == "RED")
    s.investigations = sum(1 for m in msgs if _INVESTIGATION.search(m.message.upper()))
    s.penalties = sum(
        1 for m in msgs if _PENALTY.search(m.message.upper()) and "SERVED" not in m.message.upper()
    )
    return s


def kind(m: RaceControlMessage) -> str:
    """A filter group for the timeline."""
    text, cat = m.message.upper(), (m.category or "").lower()
    if "SAFETY CAR" in text or cat == "safetycar":
        return "sc"
    if _PENALTY.search(text) or _INVESTIGATION.search(text) or "STEWARDS" in text:
        return "stewards"
    if "TRACK LIMITS" in text or "DELETED" in text:
        return "limits"
    if cat == "flag" or m.flag:
        return "flag"
    if cat == "drs" or "DRS" in text:
        return "drs"
    return "other"


# ---------------------------------------------------------------------------- views


@dataclass
class SessionRaceControl:
    session: Session
    messages: list[RaceControlMessage]
    stats: RaceControlStats

    @property
    def label(self) -> str:
        return SESSION_LABELS.get(self.session.type, self.session.type)


def race_control_for(db: DB, sessions: list[Session]) -> list[SessionRaceControl]:
    out = []
    for sess in sorted(sessions, key=lambda s: s.start_utc or datetime.max.replace(tzinfo=UTC)):
        msgs = list(
            db.scalars(
                select(RaceControlMessage)
                .where(RaceControlMessage.session_id == sess.id)
                .order_by(RaceControlMessage.at)
            )
        )
        if msgs:
            out.append(SessionRaceControl(sess, msgs, stats(msgs)))
    return out


def conditions_for(db: DB, sessions: list[Session]) -> dict[int, SessionConditions]:
    ids = [s.id for s in sessions]
    if not ids:
        return {}
    return {
        c.session_id: c
        for c in db.scalars(select(SessionConditions).where(SessionConditions.session_id.in_(ids)))
        if c.samples
    }


def _previous_races(db: DB, event: Event, limit: int) -> list[tuple[Event, Session]]:
    rows = db.execute(
        select(Event, Session)
        .join(Session, Session.event_id == Event.id)
        .where(
            Event.circuit_id == event.circuit_id,
            Event.season < event.season,
            Event.status == "scheduled",
            Session.type == "RACE",
        )
        .order_by(Event.season.desc(), Event.round.desc())
    ).all()
    return [(e, s) for e, s in rows][:limit]


@dataclass
class ScHistory:
    races: int  # races here with race control data
    with_sc: int
    with_vsc: int
    with_red: int
    seasons: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return (
            f"SC in {self.with_sc} of last {self.races} race{'s' if self.races != 1 else ''} here"
        )


def sc_history(db: DB, event: Event, last: int = 5) -> ScHistory | None:
    """Real safety car history at this circuit, from stored race control (earlier seasons only).
    Races without race control data are skipped rather than counted as 'no safety car'."""
    hist = ScHistory(0, 0, 0, 0)
    for ev, race in _previous_races(db, event, 50):
        if hist.races >= last:
            break
        fetched = db.get(SessionDataFetch, race.id)
        if fetched is None or not fetched.race_control_count:
            continue
        msgs = list(
            db.scalars(select(RaceControlMessage).where(RaceControlMessage.session_id == race.id))
        )
        st = stats(msgs)
        hist.races += 1
        hist.with_sc += st.safety_cars > 0
        hist.with_vsc += st.vscs > 0
        hist.with_red += st.red_flags > 0
        hist.seasons.append(ev.season)
    return hist if hist.races else None


@dataclass
class LastYear:
    event: Event
    session: Session
    conditions: SessionConditions


def last_year_conditions(db: DB, event: Event) -> LastYear | None:
    """Measured race conditions the last time this circuit hosted a race (earlier season)."""
    for ev, race in _previous_races(db, event, 10):
        cond = db.get(SessionConditions, race.id)
        if cond is not None and cond.samples and cond.track_min is not None:
            return LastYear(ev, race, cond)
    return None
