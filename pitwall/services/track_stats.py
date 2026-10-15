"""Track history computed from stored results. Missing data -> the stat is None (omitted)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from pitwall.models import Constructor, Driver, Event, Result, Session


@dataclass
class Podium:
    season: int
    driver: str
    constructor: str | None


@dataclass
class Tally:
    name: str
    wins: int


@dataclass
class TrackStats:
    last_winner: Podium | None = None
    last_pole: Podium | None = None
    winningest_driver: Tally | None = None
    winningest_constructor: Tally | None = None
    pole_to_win: tuple[int, int] | None = None  # (wins from pole, races with known pole)
    winner_avg_gain: tuple[float, int] | None = None  # (avg places gained, races counted)
    seasons: tuple[int, int] | None = None

    @property
    def pole_to_win_pct(self) -> int | None:
        if not self.pole_to_win or not self.pole_to_win[1]:
            return None
        return round(100 * self.pole_to_win[0] / self.pole_to_win[1])

    @property
    def empty(self) -> bool:
        return self.seasons is None


def active_ids(db: DB, season: int) -> tuple[set[str], set[str]]:
    """Drivers/constructors with results in `season`, else in the latest season with results."""
    latest = db.scalar(
        select(func.max(Event.season))
        .join(Session, Session.event_id == Event.id)
        .join(Result, Result.session_id == Session.id)
        .where(Event.season <= season)
    )
    if latest is None:
        return set(), set()
    rows = db.execute(
        select(Result.driver_id, Result.constructor_id)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(Event.season == latest)
    ).all()
    return {r[0] for r in rows}, {r[1] for r in rows if r[1]}


def compute(db: DB, event: Event) -> TrackStats:
    stats = TrackStats()
    history = list(
        db.scalars(
            select(Event)
            .where(
                Event.circuit_id == event.circuit_id,
                Event.season < event.season,
                Event.status == "scheduled",
            )
            .order_by(Event.season.desc(), Event.round.desc())
        )
    )
    if not history:
        return stats

    winners: list[tuple[Event, Result]] = []
    poles: dict[int, str] = {}
    for ev in history:
        race = next((s for s in ev.sessions if s.type == "RACE"), None)
        quali = next((s for s in ev.sessions if s.type == "Q"), None)
        win = None
        if race is not None:
            win = db.scalars(
                select(Result).where(Result.session_id == race.id, Result.position == 1)
            ).first()
            if win is not None:
                winners.append((ev, win))
        pole = None
        if quali is not None:
            pole = db.scalars(
                select(Result).where(Result.session_id == quali.id, Result.position == 1)
            ).first()
        if pole is None and race is not None:
            pole = db.scalars(
                select(Result).where(Result.session_id == race.id, Result.grid == 1)
            ).first()
        if pole is not None:
            poles[ev.id] = pole.driver_id
            if stats.last_pole is None:
                stats.last_pole = Podium(
                    ev.season,
                    pole.driver.full_name,
                    pole.constructor.name if pole.constructor else None,
                )

    if not winners and not poles:
        return stats

    seasons = [ev.season for ev, _ in winners] or [ev.season for ev in history if ev.id in poles]
    stats.seasons = (min(seasons), max(seasons))

    if winners:
        ev, win = winners[0]
        stats.last_winner = Podium(
            ev.season, win.driver.full_name, win.constructor.name if win.constructor else None
        )

    active_drivers, active_cons = active_ids(db, event.season)
    d_wins = Counter(w.driver_id for _, w in winners if w.driver_id in active_drivers)
    c_wins = Counter(
        w.constructor_id for _, w in winners if w.constructor_id and w.constructor_id in active_cons
    )
    if d_wins:
        did, n = d_wins.most_common(1)[0]
        drv = db.get(Driver, did)
        if drv:
            stats.winningest_driver = Tally(drv.full_name, n)
    if c_wins:
        cid, n = c_wins.most_common(1)[0]
        con = db.get(Constructor, cid)
        if con:
            stats.winningest_constructor = Tally(con.name, n)

    with_pole = [(ev, w) for ev, w in winners if ev.id in poles]
    if with_pole:
        converted = sum(1 for ev, w in with_pole if poles[ev.id] == w.driver_id)
        stats.pole_to_win = (converted, len(with_pole))

    gains = [w.grid - 1 for _, w in winners if w.grid is not None and w.grid > 0]
    if gains:
        stats.winner_avg_gain = (round(sum(gains) / len(gains), 1), len(gains))
    return stats
