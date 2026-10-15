"""Teammate head-to-head: qualifying record, race record and points split for a season."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Constructor, Driver, Event, Result, Session
from pitwall.services.results_util import classified


@dataclass
class Record:
    a: int = 0
    b: int = 0

    @property
    def total(self) -> int:
        return self.a + self.b


@dataclass
class Pair:
    constructor: str
    a: Driver
    b: Driver
    quali: Record = field(default_factory=Record)
    race: Record = field(default_factory=Record)
    points_a: float = 0.0
    points_b: float = 0.0
    races_together: int = 0

    @property
    def points_share_a(self) -> int | None:
        total = self.points_a + self.points_b
        return round(100 * self.points_a / total) if total else None


def season(db: DB, year: int) -> list[Pair]:
    rows = db.execute(
        select(Result, Session.type, Event.id)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(
            Event.season == year,
            Session.type.in_(("Q", "RACE", "SPRINT")),
            Result.constructor_id.is_not(None),
        )
    ).all()
    # (event, session type, constructor) -> results
    groups: dict[tuple[int, str, str], list[Result]] = defaultdict(list)
    points: dict[tuple[str, str], float] = defaultdict(float)  # (constructor, driver)
    for res, stype, event_id in rows:
        assert res.constructor_id is not None
        groups[(event_id, stype, res.constructor_id)].append(res)
        if stype in ("RACE", "SPRINT"):
            points[(res.constructor_id, res.driver_id)] += res.points or 0

    pairs: dict[tuple[str, str, str], Pair] = {}

    def pair_for(con_id: str, x: Result, y: Result) -> tuple[Pair, bool]:
        a, b = sorted((x.driver_id, y.driver_id))
        key = (con_id, a, b)
        if key not in pairs:
            con = db.get(Constructor, con_id)
            pairs[key] = Pair(con.name if con else con_id, db.get(Driver, a), db.get(Driver, b))
        return pairs[key], x.driver_id == a

    for (_event, stype, con_id), results in groups.items():
        for x, y in combinations(results, 2):
            pair, x_is_a = pair_for(con_id, x, y)
            if stype == "Q":
                if x.position is None or y.position is None:
                    continue
                rec, x_won = pair.quali, x.position < y.position
            elif stype == "RACE":
                pair.races_together += 1
                if not classified(x) and not classified(y):
                    continue  # both out: no winner
                if x.position is None or y.position is None:
                    continue
                rec, x_won = pair.race, x.position < y.position
            else:
                continue
            if x_won == x_is_a:
                rec.a += 1
            else:
                rec.b += 1

    out = []
    for (con_id, a, b), pair in pairs.items():
        if pair.races_together == 0:
            continue
        pair.points_a = points[(con_id, a)]
        pair.points_b = points[(con_id, b)]
        if pair.points_b > pair.points_a:  # list the higher scorer first
            pair.a, pair.b = pair.b, pair.a
            pair.quali = Record(pair.quali.b, pair.quali.a)
            pair.race = Record(pair.race.b, pair.race.a)
            pair.points_a, pair.points_b = pair.points_b, pair.points_a
        out.append(pair)
    out.sort(key=lambda p: -(p.points_a + p.points_b))
    return out
