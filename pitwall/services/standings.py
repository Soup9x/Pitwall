"""Standings tables with position deltas and gaps."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from pitwall.models import (
    Constructor,
    Driver,
    Event,
    FollowedDriver,
    Result,
    Session,
    StandingsSnapshot,
)


@dataclass
class StandingRow:
    position: int | None
    entity_id: str
    name: str
    points: float
    wins: int
    change: int | None  # +2 = gained two places since the previous round; None = no previous
    gap_leader: float | None
    gap_ahead: float | None
    team: str | None = None
    code: str | None = None
    followed: bool = False

    @property
    def arrow(self) -> str:
        if self.change is None or self.change == 0:
            return "–"
        return f"▲{self.change}" if self.change > 0 else f"▼{-self.change}"


@dataclass
class StandingsTable:
    season: int
    round: int
    kind: str
    rows: list[StandingRow]


def latest_season_with_standings(db: DB) -> int | None:
    return db.scalar(select(func.max(StandingsSnapshot.season)))


def fmt_points(p: float) -> str:
    return str(int(p)) if float(p).is_integer() else f"{p:g}"


def _driver_teams(db: DB, season: int) -> dict[str, str]:
    rows = db.execute(
        select(Result.driver_id, Constructor.name, Event.round)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .join(Constructor, Result.constructor_id == Constructor.id)
        .where(Event.season == season)
        .order_by(Event.round)
    ).all()
    return {r[0]: r[1] for r in rows}  # latest round wins


def table(db: DB, season: int, kind: str, rnd: int | None = None) -> StandingsTable | None:
    rounds = list(
        db.scalars(
            select(StandingsSnapshot.round)
            .where(StandingsSnapshot.season == season, StandingsSnapshot.type == kind)
            .distinct()
            .order_by(StandingsSnapshot.round)
        )
    )
    if not rounds:
        return None
    rnd = rnd if rnd in rounds else rounds[-1]
    prev_rounds = [r for r in rounds if r < rnd]
    prev = {}
    if prev_rounds:
        prev = {
            s.entity_id: s.position
            for s in db.scalars(
                select(StandingsSnapshot).where(
                    StandingsSnapshot.season == season,
                    StandingsSnapshot.type == kind,
                    StandingsSnapshot.round == prev_rounds[-1],
                )
            )
        }
    snaps = list(
        db.scalars(
            select(StandingsSnapshot)
            .where(
                StandingsSnapshot.season == season,
                StandingsSnapshot.type == kind,
                StandingsSnapshot.round == rnd,
            )
            .order_by(StandingsSnapshot.position)
        )
    )
    followed = {f.driver_id for f in db.scalars(select(FollowedDriver))}
    teams = _driver_teams(db, season) if kind == "driver" else {}

    rows: list[StandingRow] = []
    leader = snaps[0].points if snaps else 0.0
    ahead: float | None = None
    for s in snaps:
        if kind == "driver":
            drv = db.get(Driver, s.entity_id)
            name = drv.full_name if drv else s.entity_id
            code = drv.code if drv else None
        else:
            con = db.get(Constructor, s.entity_id)
            name = con.name if con else s.entity_id
            code = None
        old = prev.get(s.entity_id)
        change = (old - s.position) if (old is not None and s.position is not None) else None
        rows.append(
            StandingRow(
                position=s.position,
                entity_id=s.entity_id,
                name=name,
                points=s.points,
                wins=s.wins,
                change=change,
                gap_leader=None if s is snaps[0] else leader - s.points,
                gap_ahead=None if ahead is None else ahead - s.points,
                team=teams.get(s.entity_id),
                code=code,
                followed=s.entity_id in followed,
            )
        )
        ahead = s.points
    return StandingsTable(season, rnd, kind, rows)
