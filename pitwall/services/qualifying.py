"""Qualifying breakdown: segment times, eliminations, gaps and improvements.

Everything is derived from the stored Q1/Q2/Q3 times: a driver with no Q2 time went out in
Q1, one with a Q2 but no Q3 time went out in Q2. Nothing assumes how many cars there are or
how many drop out per segment. A time that's missing or unreadable stays None and is hidden."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Driver, Event, Result, Session
from pitwall.services import team_colors

QUALI_TYPES = ("Q", "SQ")
GRID_FROM = {"Q": "RACE", "SQ": "SPRINT"}  # the session whose grid this qualifying sets
SEGMENTS = {"Q": ("Q1", "Q2", "Q3"), "SQ": ("SQ1", "SQ2", "SQ3")}
_TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}(?:\.\d+)?)$")


def fmt_time(seconds: float) -> str:
    m, s = divmod(round(seconds, 3), 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def parse_time(text: str | None) -> float | None:
    """'1:15.912' -> 75.912 seconds. Anything else (blank, 'DNF', 'DSQ') -> None."""
    m = _TIME.match((text or "").strip())
    if not m:
        return None
    return int(m[1] or 0) * 60 + float(m[2])


@dataclass
class Row:
    driver: Driver
    team_id: str | None
    team: str | None
    color: str | None
    position: int | None
    position_text: str | None
    texts: list[str | None]  # as published, per segment
    times: list[float | None]  # seconds, per segment
    reached: int  # 1, 2 or 3: the last segment the driver took part in
    best: float | None = None
    gap_pole: float | None = None
    gap_pole_pct: float | None = None
    gap_cutoff: float | None = None  # how much faster they needed to be to go through
    teammate: Driver | None = None
    teammate_gap: float | None = None  # + slower / - faster than the teammate
    teammate_segment: str | None = None
    improvements: list[tuple[str, float]] = field(default_factory=list)  # ("Q1→Q2", -0.312)
    grid: int | None = None  # where they started, when it differs from the qualifying result
    grid_text: str | None = None
    fastest: list[bool] = field(default_factory=lambda: [False, False, False])

    @property
    def code(self) -> str:
        return (self.driver.code or self.driver.family_name[:3]).upper()


@dataclass
class Breakdown:
    session: Session
    label: str
    names: tuple[str, str, str]  # segment names: Q1-Q3, or SQ1-SQ3 for sprint qualifying
    rows: list[Row]  # every driver, in qualifying order
    groups: list[tuple[str, list[Row]]]  # ("Q3", rows), ("Out in Q2", rows), ...
    segments: int  # the deepest segment anyone reached
    pole: Row | None
    cutoffs: dict[int, float]  # segment index -> slowest time that went through
    fastest: list[float | None]
    has_grid: bool

    @property
    def timed(self) -> int:
        return sum(1 for r in self.rows if r.gap_pole is not None)

    def cutoff_text(self, segment: int) -> str | None:
        t = self.cutoffs.get(segment)
        return fmt_time(t) if t is not None else None


def _reached(texts: list[str | None]) -> int:
    if texts[2]:
        return 3
    if texts[1]:
        return 2
    return 1


def breakdown(db: DB, session: Session) -> Breakdown | None:
    results = list(
        db.scalars(
            select(Result)
            .where(Result.session_id == session.id)
            .order_by(Result.position.is_(None), Result.position)
        )
    )
    if not results or not any(r.q1 or r.q2 or r.q3 for r in results):
        return None
    season = session.event.season
    names = SEGMENTS[session.type]
    rows = [
        Row(
            driver=r.driver,
            team_id=r.constructor_id,
            team=r.constructor.name if r.constructor else None,
            color=team_colors.color(r.constructor_id, season),
            position=r.position,
            position_text=r.position_text,
            texts=[r.q1 or None, r.q2 or None, r.q3 or None],
            times=[parse_time(r.q1), parse_time(r.q2), parse_time(r.q3)],
            reached=_reached([r.q1, r.q2, r.q3]),
        )
        for r in results
    ]
    segments = max(row.reached for row in rows)
    fastest: list[float | None] = []
    for i in range(3):
        times = [t for row in rows if (t := row.times[i]) is not None]
        fastest.append(min(times) if times else None)
        for row in rows:
            row.fastest[i] = row.times[i] is not None and row.times[i] == fastest[i]

    # Cut-off: the slowest time in a segment among the drivers who made it to the next one.
    cutoffs: dict[int, float] = {}
    for i in range(segments - 1):
        through = [t for row in rows if row.reached > i + 1 and (t := row.times[i]) is not None]
        if through:
            cutoffs[i] = max(through)
    for row in rows:
        known = [t for t in row.times if t is not None]
        row.best = min(known) if known else None
        i = row.reached - 1
        if row.reached < segments and i in cutoffs and row.times[i] is not None:
            row.gap_cutoff = round(row.times[i] - cutoffs[i], 3)
        steps = [(f"{names[j]}→{names[j + 1]}", j) for j in range(row.reached - 1)]
        row.improvements = [
            (label, round(row.times[j + 1] - row.times[j], 3))
            for label, j in steps
            if row.times[j] is not None and row.times[j + 1] is not None
        ]

    pole = next((row for row in rows if row.position == 1 and row.best is not None), None)
    if pole is not None:
        for row in rows:
            if row.best is not None:
                row.gap_pole = round(row.best - pole.best, 3)
                row.gap_pole_pct = round(100 * (row.best - pole.best) / pole.best, 2)

    by_team: dict[str, list[Row]] = {}
    for row in rows:
        if row.team_id:
            by_team.setdefault(row.team_id, []).append(row)
    for mates in by_team.values():
        if len(mates) != 2:
            continue  # a mid-season swap or a third car: no fair single comparison
        for me, mate in (mates, mates[::-1]):
            me.teammate = mate.driver
            # Compare like with like: the deepest segment both set a time in.
            for i in (2, 1, 0):
                if me.times[i] is not None and mate.times[i] is not None:
                    me.teammate_gap = round(me.times[i] - mate.times[i], 3)
                    me.teammate_segment = names[i]
                    break

    has_grid = _apply_grid(db, session, rows)
    groups: list[tuple[str, list[Row]]] = []
    for reached in range(segments, 0, -1):
        members = [row for row in rows if row.reached == reached]
        if members:
            name = names[reached - 1] if reached == segments else f"Out in {names[reached - 1]}"
            groups.append((name, members))
    return Breakdown(
        session=session,
        label="Sprint qualifying" if session.type == "SQ" else "Qualifying",
        names=names,
        rows=rows,
        groups=groups,
        segments=segments,
        pole=pole,
        cutoffs=cutoffs,
        fastest=fastest,
        has_grid=has_grid,
    )


def _apply_grid(db: DB, session: Session, rows: list[Row]) -> bool:
    """Starting positions from the race (or sprint) this qualifying set, where they differ
    from the qualifying result (penalties, pit-lane starts). Only known after that race."""
    target = next((s for s in session.event.sessions if s.type == GRID_FROM[session.type]), None)
    if target is None:
        return False
    grid = {
        r.driver_id: r.grid
        for r in db.scalars(select(Result).where(Result.session_id == target.id))
        if r.grid is not None
    }
    changed = False
    for row in rows:
        g = grid.get(row.driver.id)
        if g is None or row.position is None or g == row.position:
            continue
        row.grid = g
        row.grid_text = "Pit lane" if g == 0 else f"P{g}"
        changed = True
    return changed


def sessions_with_data(db: DB, event: Event) -> list[Session]:
    """Qualifying sessions (sprint qualifying first) that have segment times stored."""
    timed = (Result.q1 != "") | (Result.q2 != "") | (Result.q3 != "")
    out = [
        s
        for s in event.sessions
        if s.type in QUALI_TYPES
        and db.scalar(select(Result.id).where(Result.session_id == s.id, timed).limit(1))
    ]
    return sorted(out, key=lambda s: QUALI_TYPES.index(s.type), reverse=True)


def chart_data(b: Breakdown, followed: set[str]) -> dict[str, Any]:
    """Gap-to-pole bars, in qualifying order. Drivers without a time are left out."""
    rows = [r for r in b.rows if r.gap_pole is not None]
    return {
        "labels": [r.code for r in rows],
        "names": [r.driver.full_name for r in rows],
        "gaps": [r.gap_pole for r in rows],
        "pct": [r.gap_pole_pct for r in rows],
        "colors": [r.color for r in rows],
        "followed": [r.driver.id in followed for r in rows],
        "reached": [r.reached for r in rows],
    }


# ---------------------------------------------------------------------------- per driver


@dataclass
class DriverQuali:
    rounds: list[dict[str, Any]]  # {round, label, name, event_id, position, reached}
    sessions: int
    avg_position: float | None
    q3: int
    segments: int  # 3 if any session had a Q3 (always, in the modern format)


def driver_season(
    db: DB, driver_id: str, season: int, *, upto_round: int | None = None
) -> DriverQuali | None:
    rows = db.execute(
        select(Result, Event)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(Event.season == season, Session.type == "Q", Result.driver_id == driver_id)
        .order_by(Event.round)
    ).all()
    rounds = []
    for res, ev in rows:
        if upto_round is not None and ev.round > upto_round:
            continue
        rounds.append(
            {
                "round": ev.round,
                "label": (ev.circuit.locality or ev.name)[:3].upper(),
                "name": ev.name,
                "event_id": ev.id,
                "position": res.position,
                "reached": _reached([res.q1, res.q2, res.q3]),
            }
        )
    if not rounds:
        return None
    positions = [r["position"] for r in rounds if r["position"] is not None]
    return DriverQuali(
        rounds=rounds,
        sessions=len(rounds),
        avg_position=round(sum(positions) / len(positions), 1) if positions else None,
        q3=sum(1 for r in rounds if r["reached"] == 3),
        segments=max(r["reached"] for r in rounds),
    )
