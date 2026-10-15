"""Builds small fictional F1 worlds directly in the DB for stats/trivia tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

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


class World:
    def __init__(self, db):
        self.db = db

    def circuit(self, cid, country, name=None, locality=None):
        c = Circuit(
            id=cid,
            name=name or cid.title() + " Circuit",
            country=country,
            locality=locality or cid.title(),
        )
        self.db.add(c)
        return c

    def driver(self, did, nationality="British", dob=date(2000, 1, 1)):
        given, family = did.split("_") if "_" in did else (did.title(), did.title())
        d = Driver(
            id=did,
            given_name=given.title(),
            family_name=family.title(),
            nationality=nationality,
            dob=dob,
            code=family[:3].upper(),
        )
        self.db.add(d)
        return d

    def con(self, cid, nationality="British", name=None):
        c = Constructor(id=cid, name=name or cid.title(), nationality=nationality)
        self.db.add(c)
        return c

    def season(self, *years, backfilled=True):
        for y in years:
            if self.db.get(Season, y) is None:
                self.db.add(Season(year=y, backfilled=backfilled))
        self.db.flush()

    def event(self, season, rnd, circuit_id, name=None, sprint=False, when=None):
        self.season(season)
        when = when or datetime(season, 3, 1, 14, tzinfo=UTC) + timedelta(days=7 * rnd)
        ev = Event(
            slug=f"{season}-{circuit_id}-{rnd}",
            season=season,
            round=rnd,
            name=name or f"{circuit_id.title()} Grand Prix",
            circuit_id=circuit_id,
            is_sprint_weekend=sprint,
            start_date=(when - timedelta(days=2)).date(),
            end_date=when.date(),
        )
        self.db.add(ev)
        self.db.flush()
        for stype, delta, dur in (("Q", -1, 1), ("RACE", 0, 2)) + (
            (("SPRINT", -1, 1),) if sprint else ()
        ):
            start = when + timedelta(days=delta, hours=-3 if stype == "SPRINT" else 0)
            ev.sessions.append(
                Session(type=stype, start_utc=start, end_utc_est=start + timedelta(hours=dur))
            )
        self.db.flush()
        return ev

    def results(self, ev, stype, rows):
        """rows: (driver, constructor, position, grid, points, status[, number])."""
        sess = next(s for s in ev.sessions if s.type == stype)
        for row in rows:
            did, cid, pos, grid, pts, status, *rest = row
            classified_ = status in ("Finished", "+1 Lap")
            self.db.add(
                Result(
                    session_id=sess.id,
                    driver_id=did,
                    constructor_id=cid,
                    position=pos,
                    grid=grid,
                    points=pts,
                    status=status,
                    position_text=str(pos) if classified_ or stype == "Q" else "R",
                    number=rest[0] if rest else None,
                )
            )
        sess.results_fetched_at = datetime.now(UTC)
        self.db.flush()
        return sess

    def standings(self, season, rnd, drivers, constructors=()):
        for i, (did, pts, wins) in enumerate(drivers, 1):
            self.db.add(
                StandingsSnapshot(
                    season=season,
                    round=rnd,
                    type="driver",
                    entity_id=did,
                    position=i,
                    points=pts,
                    wins=wins,
                )
            )
        for i, (cid, pts, wins) in enumerate(constructors, 1):
            self.db.add(
                StandingsSnapshot(
                    season=season,
                    round=rnd,
                    type="constructor",
                    entity_id=cid,
                    position=i,
                    points=pts,
                    wins=wins,
                )
            )
        self.db.flush()
