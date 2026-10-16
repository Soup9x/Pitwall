"""Circuit explorer: every circuit Pitwall knows, and the full history of races held there.

Everything comes from stored results (Jolpica) and, for 2023 onwards, stored race control and
track conditions (OpenF1). Races are grouped by circuit id, not Grand Prix name, so e.g. the
Eifel GP at the Nürburgring sits with the German GPs held there. Unknown values stay None and
are hidden. The fastest race lap in the data is deliberately kept apart from the official lap
record: layouts change, so the computed number is never called the record."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DB

from pitwall.models import (
    Circuit,
    Constructor,
    Driver,
    Event,
    RaceControlMessage,
    Result,
    Session,
    SessionConditions,
    SessionDataFetch,
)
from pitwall.services import results_util, session_data, track_stats
from pitwall.services.qualifying import parse_time

FILTERS = ("all", "current", "historical")


def _held(ev: Event, today: date, with_results: set[int]) -> bool:
    """Whether the race has taken place (old seasons have dates but no session times)."""
    return ev.status == "scheduled" and (
        ev.id in with_results or (ev.end_date is not None and ev.end_date < today)
    )


def _events_with_results(db: DB) -> set[int]:
    return set(
        db.scalars(
            select(Session.event_id)
            .join(Result, Result.session_id == Session.id)
            .where(Session.type == "RACE")
            .distinct()
        )
    )


# ---------------------------------------------------------------------------- list


@dataclass
class CircuitSummary:
    circuit: Circuit
    current_round: int | None  # round on the current calendar, if it's on it
    races: int  # Grands Prix held here that Pitwall knows about
    first: int | None
    last: int | None


def listing(
    db: DB,
    season: int,
    now: datetime,
    *,
    query: str = "",
    which: str = "all",
    hidden: set[int] | None = None,
) -> list[CircuitSummary]:
    """Circuits with at least one event, current-calendar circuits first (by round)."""
    hidden = hidden or set()
    today = now.date()
    with_results = _events_with_results(db)
    by_circuit: dict[str, list[Event]] = {}
    for ev in db.scalars(select(Event).order_by(Event.season, Event.round)):
        by_circuit.setdefault(ev.circuit_id, []).append(ev)
    circuits = {c.id: c for c in db.scalars(select(Circuit))}
    q = query.strip().lower()
    out = []
    for cid, events in by_circuit.items():
        c = circuits.get(cid)
        if c is None:
            continue
        if q and not any(q in (v or "").lower() for v in (c.name, c.locality, c.country, c.id)):
            continue
        current = next((e for e in events if e.season == season and e.status == "scheduled"), None)
        if (which == "current" and current is None) or (which == "historical" and current):
            continue
        held = [e for e in events if e.id not in hidden and _held(e, today, with_results)]
        seasons = [e.season for e in held]
        out.append(
            CircuitSummary(
                c,
                current.round if current else None,
                len(held),
                min(seasons) if seasons else None,
                max(seasons) if seasons else None,
            )
        )
    out.sort(
        key=lambda s: (
            s.current_round is None,
            s.current_round or 0,
            -(s.last or 0),
            s.circuit.name,
        )
    )
    return out


# ---------------------------------------------------------------------------- detail


@dataclass
class RaceRow:
    event: Event
    winner: Result | None = None
    pole: Result | None = None  # qualifying P1, else the driver who started from grid 1
    fastest: Result | None = None  # fastest lap of the race (Jolpica has these from 2004)


@dataclass
class Leader:
    id: str
    name: str
    count: int
    active: bool


@dataclass
class ScRow:
    season: int
    safety_cars: int
    vscs: int
    red_flags: int


@dataclass
class WeatherRow:
    season: int
    conditions: SessionConditions


@dataclass
class FastestLap:
    time: str
    driver: Driver
    season: int
    event: Event


@dataclass
class CircuitDetail:
    circuit: Circuit
    current: Event | None  # this season's event here, if any
    last_held: Event | None
    races: list[RaceRow]  # newest first
    fastest_lap: FastestLap | None = None
    wins: list[Leader] = field(default_factory=list)
    poles: list[Leader] = field(default_factory=list)
    podiums: list[Leader] = field(default_factory=list)
    team_wins: list[Leader] = field(default_factory=list)
    pole_to_win: tuple[int, int] | None = None  # (wins from pole, races with known pole+winner)
    winner_grid: tuple[float, int] | None = None  # (average grid, races counted)
    dnf: tuple[int, int] | None = None  # (retirements, starters)
    sc: list[ScRow] = field(default_factory=list)
    weather: list[WeatherRow] = field(default_factory=list)

    @property
    def pole_to_win_pct(self) -> int | None:
        if not self.pole_to_win or not self.pole_to_win[1]:
            return None
        return round(100 * self.pole_to_win[0] / self.pole_to_win[1])

    @property
    def dnf_pct(self) -> float | None:
        if not self.dnf or not self.dnf[1]:
            return None
        return round(100 * self.dnf[0] / self.dnf[1], 1)


def _leaders(
    counts: Counter[str], names: dict[str, str], active: set[str], n: int = 5
) -> list[Leader]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], names.get(kv[0], kv[0])))
    return [Leader(k, names.get(k, k), v, k in active) for k, v in ranked[:n]]


def detail(
    db: DB, circuit_id: str, season: int, now: datetime, *, hidden: set[int] | None = None
) -> CircuitDetail | None:
    """`hidden`: event ids spoiler mode holds back; they're left out entirely."""
    circuit = db.get(Circuit, circuit_id)
    if circuit is None:
        return None
    hidden = hidden or set()
    today = now.date()
    with_results = _events_with_results(db)
    events = list(
        db.scalars(
            select(Event)
            .where(Event.circuit_id == circuit_id)
            .order_by(Event.season.desc(), Event.round.desc())
        )
    )
    current = next((e for e in events if e.season == season and e.status == "scheduled"), None)
    held = [e for e in events if e.id not in hidden and _held(e, today, with_results)]
    detail = CircuitDetail(circuit, current, held[0] if held else None, [RaceRow(e) for e in held])
    if not held:
        return detail

    sessions = {
        s.id: s
        for s in db.scalars(
            select(Session).where(
                Session.event_id.in_([e.id for e in held]), Session.type.in_(("RACE", "Q"))
            )
        )
    }
    race_ids = [sid for sid, s in sessions.items() if s.type == "RACE"]
    quali_ids = [sid for sid, s in sessions.items() if s.type == "Q"]
    rows = {r.event.id: r for r in detail.races}
    race_results = list(db.scalars(select(Result).where(Result.session_id.in_(race_ids))))
    pole_from_q = {
        sessions[r.session_id].event_id: r
        for r in db.scalars(
            select(Result).where(Result.session_id.in_(quali_ids), Result.position == 1)
        )
    }

    wins: Counter[str] = Counter()
    team_wins: Counter[str] = Counter()
    poles: Counter[str] = Counter()
    podiums: Counter[str] = Counter()
    retired = starters = 0
    best: tuple[float, int, Result] | None = None  # (seconds, season, result): ties go to the first
    for r in race_results:
        row = rows[sessions[r.session_id].event_id]
        if r.position == 1:
            row.winner = r
            wins[r.driver_id] += 1
            if r.constructor_id:
                team_wins[r.constructor_id] += 1
        if r.position is not None and r.position <= 3 and results_util.classified(r):
            podiums[r.driver_id] += 1
        if r.grid == 1 and row.event.id not in pole_from_q:
            row.pole = r
        if r.fastest_lap_rank == 1:
            row.fastest = r
        if r.status and results_util.started(r):
            starters += 1
            retired += results_util.dnf(r)
        t = parse_time(r.fastest_lap_time)
        if t is not None and (best is None or (t, row.event.season) < best[:2]):
            best = (t, row.event.season, r)
    for ev_id, r in pole_from_q.items():
        rows[ev_id].pole = r
    for row in detail.races:
        if row.pole is not None:
            poles[row.pole.driver_id] += 1

    active_drivers, active_teams = track_stats.active_ids(db, season)
    driver_names = {
        d.id: d.full_name
        for d in db.scalars(
            select(Driver).where(Driver.id.in_(set(wins) | set(poles) | set(podiums)))
        )
    }
    team_names = {
        c.id: c.name for c in db.scalars(select(Constructor).where(Constructor.id.in_(team_wins)))
    }
    detail.wins = _leaders(wins, driver_names, active_drivers)
    detail.poles = _leaders(poles, driver_names, active_drivers)
    detail.podiums = _leaders(podiums, driver_names, active_drivers)
    detail.team_wins = _leaders(team_wins, team_names, active_teams)

    both = [r for r in detail.races if r.pole is not None and r.winner is not None]
    if both:
        detail.pole_to_win = (sum(r.pole.driver_id == r.winner.driver_id for r in both), len(both))
    grids = [r.winner.grid for r in detail.races if r.winner and r.winner.grid]  # 0 = pit lane
    if grids:
        detail.winner_grid = (round(sum(grids) / len(grids), 1), len(grids))
    if starters:
        detail.dnf = (retired, starters)
    if best is not None:
        r = best[2]
        ev = rows[sessions[r.session_id].event_id].event
        detail.fastest_lap = FastestLap(r.fastest_lap_time or "", r.driver, ev.season, ev)

    season_of = {
        sid: next(e.season for e in held if e.id == s.event_id) for sid, s in sessions.items()
    }
    fetched = {
        f.session_id
        for f in db.scalars(
            select(SessionDataFetch).where(
                SessionDataFetch.session_id.in_(race_ids), SessionDataFetch.race_control_count > 0
            )
        )
    }
    for sid in sorted(fetched, key=lambda s: -season_of[s]):
        msgs = list(
            db.scalars(select(RaceControlMessage).where(RaceControlMessage.session_id == sid))
        )
        st = session_data.stats(msgs)
        detail.sc.append(ScRow(season_of[sid], st.safety_cars, st.vscs, st.red_flags))
    for cond in db.scalars(
        select(SessionConditions).where(
            SessionConditions.session_id.in_(race_ids),
            or_(SessionConditions.track_min.is_not(None), SessionConditions.air_min.is_not(None)),
        )
    ):
        if cond.samples:
            detail.weather.append(WeatherRow(season_of[cond.session_id], cond))
    detail.weather.sort(key=lambda w: -w.season)
    return detail
