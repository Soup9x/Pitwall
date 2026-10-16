"""Constructor pages: a team's season (results, drivers, points trend) and its history.

Computed only from stored results; the championship position and points come from the
official standings snapshot when there is one. Unknown values stay None and are hidden."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Constructor, Driver, Event, Result, Session, StandingsSnapshot
from pitwall.services import team_colors
from pitwall.services.qualifying import _reached
from pitwall.services.results_util import classified, dnf


@dataclass
class CarResult:
    driver: Driver
    finish: str  # "P3", "R", "DSQ", ...
    position: int | None  # classified finishing position
    grid: int | None
    points: float  # race + sprint points this weekend
    dnf: bool
    status: str


@dataclass
class TeamRound:
    event: Event
    cars: list[CarResult]
    points: float = 0.0
    cumulative: float = 0.0


@dataclass
class DriverLine:
    driver: Driver
    races: int = 0
    points: float = 0.0
    wins: int = 0
    podiums: int = 0
    best: int | None = None
    finishes: list[int] = field(default_factory=list)
    quali: list[int] = field(default_factory=list)
    cumulative: list[float] = field(default_factory=list)

    @property
    def avg_finish(self) -> float | None:
        return round(sum(self.finishes) / len(self.finishes), 1) if self.finishes else None

    @property
    def avg_quali(self) -> float | None:
        return round(sum(self.quali) / len(self.quali), 1) if self.quali else None


@dataclass
class TeamSeason:
    constructor: Constructor
    season: int
    color: str | None
    rounds: list[TeamRound]
    drivers: list[DriverLine]
    standing_position: int | None = None
    standing_points: float | None = None
    standing_round: int | None = None
    wins: int = 0
    podiums: int = 0
    one_twos: int = 0
    double_points: int = 0  # both cars scored
    poles: int = 0
    front_rows: int = 0
    q3_cars: int = 0
    quali_sessions: int = 0
    dnfs: int = 0
    starts: int = 0

    @property
    def points(self) -> float:
        return self.rounds[-1].cumulative if self.rounds else 0.0

    @property
    def best_finish(self) -> int | None:
        found = [c.position for r in self.rounds for c in r.cars if c.position is not None]
        return min(found) if found else None


@dataclass
class HistoryRow:
    season: int
    races: int
    wins: int
    podiums: int
    points: float
    position: int | None  # final (latest) championship position stored for that season


def seasons_for(db: DB, constructor_id: str) -> list[int]:
    return list(
        db.scalars(
            select(Event.season)
            .join(Session)
            .join(Result, Result.session_id == Session.id)
            .where(Result.constructor_id == constructor_id)
            .distinct()
            .order_by(Event.season.desc())
        )
    )


def _finish(r: Result) -> str:
    return f"P{r.position_text}" if classified(r) else (r.position_text or "?")


def season(db: DB, constructor_id: str, year: int) -> TeamSeason | None:
    team = db.get(Constructor, constructor_id)
    if team is None:
        return None
    rows = db.execute(
        select(Result, Session, Event)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(
            Result.constructor_id == constructor_id,
            Event.season == year,
            Session.type.in_(("RACE", "SPRINT", "Q")),
        )
        .order_by(Event.round, Result.position)
    ).all()
    data = TeamSeason(team, year, team_colors.color(constructor_id, year), [], [])
    rounds: dict[int, TeamRound] = {}
    sprint: dict[tuple[int, str], float] = defaultdict(float)
    lines: dict[str, DriverLine] = {}
    quali_by_event: dict[int, list[Result]] = defaultdict(list)

    def line(d: Driver) -> DriverLine:
        if d.id not in lines:
            lines[d.id] = DriverLine(d)
        return lines[d.id]

    for res, sess, ev in rows:
        if sess.type == "SPRINT":
            sprint[(ev.id, res.driver_id)] += res.points or 0.0
            continue
        if sess.type == "Q":
            quali_by_event[ev.id].append(res)
            if res.position is not None:
                line(res.driver).quali.append(res.position)
            continue
        tr = rounds.setdefault(ev.id, TeamRound(ev, []))
        car = CarResult(
            res.driver,
            _finish(res),
            res.position if classified(res) else None,
            res.grid or None,
            res.points or 0.0,
            dnf(res),
            res.status or "",
        )
        tr.cars.append(car)
        dl = line(res.driver)
        dl.races += 1
        if car.position is not None:
            dl.finishes.append(car.position)
            dl.best = car.position if dl.best is None else min(dl.best, car.position)
            dl.wins += car.position == 1
            dl.podiums += car.position <= 3
        data.starts += 1
        data.dnfs += car.dnf

    total = 0.0
    driver_totals: dict[str, float] = defaultdict(float)
    for tr in sorted(rounds.values(), key=lambda r: r.event.round):
        for car in tr.cars:
            car.points += sprint.pop((tr.event.id, car.driver.id), 0.0)
        # A sprint scored by a driver with no race result that weekend still counts.
        extra = {d: p for (e, d), p in sprint.items() if e == tr.event.id}
        for d, p in extra.items():
            driver_totals[d] += p
        for car in tr.cars:
            driver_totals[car.driver.id] += car.points
        tr.points = sum(c.points for c in tr.cars) + sum(extra.values())
        total += tr.points
        tr.cumulative = total
        positions = sorted(c.position for c in tr.cars if c.position is not None)
        data.wins += 1 in positions
        data.podiums += sum(1 for p in positions if p <= 3)
        data.one_twos += positions[:2] == [1, 2]
        data.double_points += len(tr.cars) >= 2 and all(c.points > 0 for c in tr.cars)
        for dl in lines.values():
            dl.cumulative.append(driver_totals[dl.driver.id])
        data.rounds.append(tr)
    for dl in lines.values():
        dl.points = driver_totals[dl.driver.id]

    for results in quali_by_event.values():
        data.quali_sessions += 1
        positions = [r.position for r in results if r.position is not None]
        data.poles += 1 in positions
        data.front_rows += any(p <= 2 for p in positions)
        data.q3_cars += sum(1 for r in results if _reached([r.q1, r.q2, r.q3]) == 3)

    snap = db.execute(
        select(StandingsSnapshot)
        .where(
            StandingsSnapshot.season == year,
            StandingsSnapshot.type == "constructor",
            StandingsSnapshot.entity_id == constructor_id,
        )
        .order_by(StandingsSnapshot.round.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snap is not None:
        data.standing_position = snap.position
        data.standing_points = snap.points
        data.standing_round = snap.round

    data.drivers = sorted(
        (dl for dl in lines.values() if dl.races or dl.quali),
        key=lambda d: (-d.points, -d.races, d.driver.family_name),
    )
    return data


def history(db: DB, constructor_id: str, *, upto_season: int | None = None) -> list[HistoryRow]:
    """One row per season with race results, newest first."""
    out: dict[int, HistoryRow] = {}
    rows = db.execute(
        select(Event.season, Event.id, Session.type, Result)
        .join(Session, Session.event_id == Event.id)
        .join(Result, Result.session_id == Session.id)
        .where(Result.constructor_id == constructor_id, Session.type.in_(("RACE", "SPRINT")))
    ).all()
    races: dict[int, set[int]] = defaultdict(set)
    wins: dict[int, set[int]] = defaultdict(set)
    for year, event_id, stype, res in rows:
        if upto_season is not None and year > upto_season:
            continue
        row = out.setdefault(year, HistoryRow(year, 0, 0, 0, 0.0, None))
        row.points += res.points or 0.0
        if stype != "RACE":
            continue
        races[year].add(event_id)
        if res.position is not None and classified(res):
            if res.position == 1:
                wins[year].add(event_id)
            row.podiums += res.position <= 3
    for year, row in out.items():
        row.races, row.wins = len(races[year]), len(wins[year])
    for year, pos in db.execute(
        select(StandingsSnapshot.season, StandingsSnapshot.position)
        .where(
            StandingsSnapshot.type == "constructor",
            StandingsSnapshot.entity_id == constructor_id,
        )
        .order_by(StandingsSnapshot.season, StandingsSnapshot.round)
    ).all():
        if year in out:
            out[year].position = pos  # the latest round wins
    return [out[y] for y in sorted(out, reverse=True) if out[y].races]
