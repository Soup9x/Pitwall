"""Jolpica sync: schedule (with change detection), results, standings and historical backfill.

Each sync is split into a *fetch* step (network, may fail) and an *apply* step (pure DB work),
so the DB is never locked while waiting on the network and the apply steps are easy to test."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import (
    Circuit,
    Constructor,
    Driver,
    Event,
    Result,
    Season,
    Session,
    StandingsSnapshot,
)
from pitwall.services.circuits import apply_curated, load_curated

log = logging.getLogger(__name__)

# Ergast schedule keys -> our session types
SCHEDULE_KEYS = {
    "FirstPractice": "FP1",
    "SecondPractice": "FP2",
    "ThirdPractice": "FP3",
    "SprintQualifying": "SQ",
    "SprintShootout": "SQ",
    "Sprint": "SPRINT",
    "Qualifying": "Q",
}
# Estimated session lengths, used for end times (ICS) and the results-poll window.
SESSION_DURATION = {
    "FP1": timedelta(minutes=60),
    "FP2": timedelta(minutes=60),
    "FP3": timedelta(minutes=60),
    "SQ": timedelta(minutes=45),
    "SPRINT": timedelta(minutes=60),
    "Q": timedelta(minutes=60),
    "RACE": timedelta(minutes=120),
}


# ---------------------------------------------------------------------------- parsing helpers


def parse_dt(d: str | None, t: str | None) -> datetime | None:
    if not d or not t:
        return None
    t = t.rstrip("Z")
    try:
        return datetime.fromisoformat(f"{d}T{t}+00:00")
    except ValueError:
        return None


def parse_date(d: str | None) -> date | None:
    try:
        return date.fromisoformat(d) if d else None
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def race_sessions(race: dict[str, Any]) -> dict[str, tuple[date | None, datetime | None]]:
    """Map session type -> (date, start_utc) from an Ergast race schedule entry."""
    out: dict[str, tuple[date | None, datetime | None]] = {}
    for key, stype in SCHEDULE_KEYS.items():
        info = race.get(key)
        if isinstance(info, dict):
            out[stype] = (
                parse_date(info.get("date")),
                parse_dt(info.get("date"), info.get("time")),
            )
    out["RACE"] = (parse_date(race.get("date")), parse_dt(race.get("date"), race.get("time")))
    return out


def upsert_circuit(db: DB, c: dict[str, Any], curated: dict[str, dict[str, Any]]) -> Circuit:
    circuit = db.get(Circuit, c["circuitId"])
    if circuit is None:
        circuit = Circuit(id=c["circuitId"], name=c.get("circuitName", c["circuitId"]))
        db.add(circuit)
    loc = c.get("Location", {})
    circuit.name = c.get("circuitName", circuit.name)
    circuit.locality = loc.get("locality")
    circuit.country = loc.get("country")
    circuit.lat = _float(loc.get("lat"))
    circuit.lng = _float(loc.get("long"))
    circuit.url = c.get("url")
    if circuit.id in curated:
        apply_curated(circuit, curated[circuit.id])
    return circuit


def upsert_driver(db: DB, d: dict[str, Any]) -> Driver:
    drv = db.get(Driver, d["driverId"])
    if drv is None:
        drv = Driver(
            id=d["driverId"], given_name=d.get("givenName", ""), family_name=d.get("familyName", "")
        )
        db.add(drv)
    drv.given_name = d.get("givenName", drv.given_name)
    drv.family_name = d.get("familyName", drv.family_name)
    drv.code = d.get("code") or drv.code
    drv.number = _int(d.get("permanentNumber")) or drv.number
    drv.dob = parse_date(d.get("dateOfBirth")) or drv.dob
    drv.nationality = d.get("nationality") or drv.nationality
    drv.url = d.get("url") or drv.url
    return drv


def upsert_constructor(db: DB, c: dict[str, Any]) -> Constructor:
    con = db.get(Constructor, c["constructorId"])
    if con is None:
        con = Constructor(id=c["constructorId"], name=c.get("name", c["constructorId"]))
        db.add(con)
    con.name = c.get("name", con.name)
    con.nationality = c.get("nationality") or con.nationality
    con.url = c.get("url") or con.url
    return con


# ---------------------------------------------------------------------------- schedule


@dataclass
class SessionChange:
    kind: str  # moved | confirmed | added | cancelled
    session_type: str
    old_start: datetime | None = None
    new_start: datetime | None = None


@dataclass
class EventChange:
    event_id: int
    kind: str  # changed | added | cancelled
    sessions: list[SessionChange] = field(default_factory=list)


@dataclass
class ScheduleResult:
    year: int
    events: int = 0
    changes: list[EventChange] = field(default_factory=list)
    first_import: bool = False


def _is_future(now: datetime, *dts: datetime | None) -> bool:
    return any(dt is not None and dt > now for dt in dts)


def apply_schedule(
    db: DB,
    year: int,
    races: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    detect_changes: bool = True,
    curated: dict[str, dict[str, Any]] | None = None,
) -> ScheduleResult:
    """Upsert a season's calendar and report changes to *future* sessions.

    Events are matched by (circuit, nth visit) rather than round number, so a cancelled race
    that causes Jolpica to renumber later rounds doesn't look like every race moved."""
    now = now or utcnow()
    curated = load_curated() if curated is None else curated
    result = ScheduleResult(year=year, events=len(races))

    if db.get(Season, year) is None:
        db.add(Season(year=year))
        db.flush()

    existing = list(db.scalars(select(Event).where(Event.season == year).order_by(Event.round)))
    result.first_import = not existing
    detect = detect_changes and not result.first_import

    by_slug = {e.slug: e for e in existing}
    seen_slugs: set[str] = set()
    visits: dict[str, int] = defaultdict(int)

    for race in races:
        circuit = upsert_circuit(db, race["Circuit"], curated)
        visits[circuit.id] += 1
        slug = f"{year}-{circuit.id}" + (f"-{visits[circuit.id]}" if visits[circuit.id] > 1 else "")
        seen_slugs.add(slug)
        sessions = race_sessions(race)
        dates = [d for d, _ in sessions.values() if d]

        event = by_slug.get(slug)
        event_change: EventChange | None = None
        is_new_event = event is None
        if event is None:
            event = Event(
                slug=slug,
                season=year,
                round=int(race["round"]),
                name=race["raceName"],
                circuit_id=circuit.id,
            )
            db.add(event)
            db.flush()
            if detect and _is_future(now, *(s for _, s in sessions.values())):
                event_change = EventChange(event.id, "added")
        elif event.status == "cancelled":
            if detect and _is_future(now, *(s for _, s in sessions.values())):
                event_change = EventChange(event.id, "added")
        event.round = int(race["round"])
        event.name = race["raceName"]
        event.circuit_id = circuit.id
        event.status = "scheduled"
        event.url = race.get("url")
        event.is_sprint_weekend = "SPRINT" in sessions
        event.start_date = min(dates) if dates else None
        event.end_date = sessions["RACE"][0]

        existing_sessions = {s.type: s for s in event.sessions} if not is_new_event else {}
        session_changes: list[SessionChange] = []
        for stype, (_d, start) in sessions.items():
            end = start + SESSION_DURATION[stype] if start else None
            sess = existing_sessions.get(stype)
            if sess is None:
                event.sessions.append(
                    Session(type=stype, start_utc=start, end_utc_est=end, last_changed_at=now)
                )
                if detect and not is_new_event and _is_future(now, start):
                    session_changes.append(SessionChange("added", stype, None, start))
                continue
            reinstated = sess.status == "cancelled"
            if sess.start_utc != start or reinstated:
                kind = (
                    "added" if reinstated else ("confirmed" if sess.start_utc is None else "moved")
                )
                if detect and _is_future(now, sess.start_utc, start):
                    session_changes.append(SessionChange(kind, stype, sess.start_utc, start))
                sess.start_utc = start
                sess.end_utc_est = end
                sess.status = "scheduled"
                sess.sequence += 1
                sess.last_changed_at = now
        for stype, sess in existing_sessions.items():
            if stype not in sessions and sess.status != "cancelled":
                if detect and _is_future(now, sess.start_utc):
                    session_changes.append(SessionChange("cancelled", stype, sess.start_utc, None))
                sess.status = "cancelled"
                sess.sequence += 1
                sess.last_changed_at = now

        if event_change is None and session_changes:
            event_change = EventChange(event.id, "changed")
        if event_change is not None:
            event_change.sessions = session_changes
            result.changes.append(event_change)

    for slug, event in by_slug.items():
        if slug in seen_slugs or event.status == "cancelled":
            continue
        future = [s for s in event.sessions if _is_future(now, s.start_utc)]
        event.status = "cancelled"
        for sess in event.sessions:
            if sess.status != "cancelled":
                sess.status = "cancelled"
                sess.sequence += 1
                sess.last_changed_at = now
        if detect and future:
            result.changes.append(
                EventChange(
                    event.id,
                    "cancelled",
                    [SessionChange("cancelled", s.type, s.start_utc, None) for s in future],
                )
            )

    season = db.get(Season, year)
    if season is not None:
        season.last_synced_at = now
    db.flush()
    return result


# ---------------------------------------------------------------------------- results


RESULT_LIST_KEY = {"RACE": "Results", "Q": "QualifyingResults", "SPRINT": "SprintResults"}


def find_event(db: DB, season: int, rnd: int) -> Event | None:
    return db.scalars(
        select(Event).where(Event.season == season, Event.round == rnd, Event.status == "scheduled")
    ).first()


def apply_results(
    db: DB, race: dict[str, Any], stype: str, *, now: datetime | None = None
) -> Session | None:
    """Store results for one session from an Ergast race dict. Returns the session, or None if
    the race has no rows (results not published yet) or the event is unknown."""
    rows = race.get(RESULT_LIST_KEY[stype]) or []
    if not rows:
        return None
    event = find_event(db, int(race["season"]), int(race["round"]))
    if event is None:
        log.warning(
            "results for unknown event",
            extra={"season": race.get("season"), "round": race.get("round")},
        )
        return None
    sess = next((s for s in event.sessions if s.type == stype), None)
    if sess is None:
        sess = Session(type=stype, last_changed_at=now or utcnow())
        event.sessions.append(sess)
        db.flush()
    db.query(Result).filter(Result.session_id == sess.id).delete()
    for row in rows:
        drv = upsert_driver(db, row["Driver"])
        con = upsert_constructor(db, row["Constructor"]) if row.get("Constructor") else None
        res = Result(
            session_id=sess.id,
            driver_id=drv.id,
            constructor_id=con.id if con else None,
            number=_int(row.get("number")),
            position=_int(row.get("position")),
            position_text=row.get("positionText"),
        )
        if stype == "Q":
            res.q1, res.q2, res.q3 = row.get("Q1"), row.get("Q2"), row.get("Q3")
        else:
            res.grid = _int(row.get("grid"))
            res.status = row.get("status")
            res.points = _float(row.get("points")) or 0.0
            res.laps = _int(row.get("laps"))
            res.time_text = (row.get("Time") or {}).get("time")
            fl = row.get("FastestLap") or {}
            res.fastest_lap_rank = _int(fl.get("rank"))
            res.fastest_lap_time = (fl.get("Time") or {}).get("time")
        db.add(res)
    sess.results_fetched_at = now or utcnow()
    db.flush()
    return sess


# ---------------------------------------------------------------------------- standings


@dataclass
class StandingsUpdate:
    season: int
    round: int
    changed: bool


def apply_standings(
    db: DB, kind: str, standings_list: dict[str, Any], *, now: datetime | None = None
) -> StandingsUpdate:
    season = int(standings_list["season"])
    rnd = int(standings_list["round"])
    key = "DriverStandings" if kind == "driver" else "ConstructorStandings"
    existing = {
        s.entity_id: s
        for s in db.scalars(
            select(StandingsSnapshot).where(
                StandingsSnapshot.season == season,
                StandingsSnapshot.round == rnd,
                StandingsSnapshot.type == kind,
            )
        )
    }
    changed = not existing
    for row in standings_list.get(key, []):
        if kind == "driver":
            entity = upsert_driver(db, row["Driver"]).id
            for con in row.get("Constructors", []):
                upsert_constructor(db, con)
        else:
            entity = upsert_constructor(db, row["Constructor"]).id
        pos = _int(row.get("position"))
        pts = _float(row.get("points")) or 0.0
        wins = _int(row.get("wins")) or 0
        snap = existing.get(entity)
        if snap is None:
            db.add(
                StandingsSnapshot(
                    season=season,
                    round=rnd,
                    type=kind,
                    entity_id=entity,
                    position=pos,
                    points=pts,
                    wins=wins,
                    taken_at=now or utcnow(),
                )
            )
            changed = True
        elif (snap.position, snap.points, snap.wins) != (pos, pts, wins):
            snap.position, snap.points, snap.wins = pos, pts, wins
            snap.taken_at = now or utcnow()
            changed = True
    db.flush()
    return StandingsUpdate(season, rnd, changed)
