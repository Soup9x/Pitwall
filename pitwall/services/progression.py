"""Season progression: cumulative championship points after every round.

Every number comes from a stored standings snapshot. Rounds without one are fetched from
Jolpica (`backfill`) in the background; until then they're simply missing from the chart,
never estimated. Points are what the official standings said after each round, so sprint
points are included in the round they were scored."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import session_scope, utcnow
from pitwall.models import (
    Constructor,
    Driver,
    Event,
    FollowedDriver,
    Session,
    StandingsSnapshot,
)
from pitwall.services import standings as standings_svc
from pitwall.services import team_colors
from pitwall.services.sync import apply_standings
from pitwall.settings_store import get_kv, set_kv
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.jolpica import JolpicaClient

log = logging.getLogger(__name__)
QUEUE_KEY = "progression_queue"
SETTLE = timedelta(hours=3)  # standings are published a while after the race
KINDS = ("driver", "constructor")


@dataclass
class RoundInfo:
    round: int
    label: str  # short name for the axis, e.g. "MEX"
    name: str
    sprint: bool


@dataclass
class Series:
    id: str
    name: str
    code: str
    color: str | None
    followed: bool
    points: list[float | None] = field(default_factory=list)
    final_position: int | None = None


def completed_rounds(db: DB, season: int, now: datetime | None = None) -> list[Event]:
    """Events of the season whose race finished at least SETTLE ago, by round."""
    now = now or utcnow()
    return list(
        db.scalars(
            select(Event)
            .join(Session, Session.event_id == Event.id)
            .where(
                Event.season == season,
                Event.status == "scheduled",
                Session.type == "RACE",
                Session.end_utc_est.is_not(None),
                Session.end_utc_est < now - SETTLE,
            )
            .order_by(Event.round)
        )
    )


def _have(db: DB, season: int, kind: str) -> set[int]:
    return set(
        db.scalars(
            select(StandingsSnapshot.round)
            .where(StandingsSnapshot.season == season, StandingsSnapshot.type == kind)
            .distinct()
        )
    )


def missing_rounds(db: DB, season: int, now: datetime | None = None) -> dict[str, list[int]]:
    rounds = [e.round for e in completed_rounds(db, season, now)]
    return {kind: [r for r in rounds if r not in _have(db, season, kind)] for kind in KINDS}


def _label(ev: Event) -> str:
    loc = ev.circuit.locality or ev.circuit.name or ev.name
    return loc[:3].upper()


def build(
    db: DB,
    season: int,
    kind: str,
    *,
    user_id: int,
    upto_round: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Chart data. `upto_round` (inclusive) cuts the chart short, e.g. for spoiler mode."""
    events = [
        e for e in completed_rounds(db, season, now) if upto_round is None or e.round <= upto_round
    ]
    rounds = [RoundInfo(e.round, _label(e), e.name, e.is_sprint_weekend) for e in events]
    index = {r.round: i for i, r in enumerate(rounds)}
    snaps = db.scalars(
        select(StandingsSnapshot).where(
            StandingsSnapshot.season == season,
            StandingsSnapshot.type == kind,
            StandingsSnapshot.round.in_(list(index)),
        )
    )
    followed = {
        f.driver_id
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user_id))
    }
    teams = standings_svc._driver_teams(db, season)
    series: dict[str, Series] = {}
    last_round: dict[str, int] = {}
    for s in snaps:
        sr = series.get(s.entity_id)
        if sr is None:
            if kind == "driver":
                d = db.get(Driver, s.entity_id)
                name = d.full_name if d else s.entity_id
                code = (
                    d.code if d and d.code else (d.family_name[:3] if d else s.entity_id[:3])
                ).upper()
                team_id = teams.get(s.entity_id, (None, None))[0]
            else:
                c = db.get(Constructor, s.entity_id)
                name = c.name if c else s.entity_id
                code = name[:3].upper()
                team_id = s.entity_id
            sr = Series(
                s.entity_id,
                name,
                code,
                team_colors.color(team_id, season),
                s.entity_id in followed
                or (
                    kind == "constructor"
                    and any(teams.get(d, (None,))[0] == s.entity_id for d in followed)
                ),
                [None] * len(rounds),
            )
            series[s.entity_id] = sr
        sr.points[index[s.round]] = s.points
        if s.round >= last_round.get(s.entity_id, 0):
            last_round[s.entity_id] = s.round
            sr.final_position = s.position
    ordered = sorted(series.values(), key=lambda x: (x.final_position or 999, x.name))
    have = sorted(_have(db, season, kind) & set(index))
    return {
        "season": season,
        "kind": kind,
        "rounds": [r.__dict__ for r in rounds],
        "series": [s.__dict__ for s in ordered],
        "missing": [r.round for r in rounds if r.round not in have],
    }


# ---------------------------------------------------------------------------- backfill


def request(db: DB, season: int) -> None:
    """Ask the background job to fill in this season's missing rounds."""
    queue = list(get_kv(db, QUEUE_KEY) or [])
    if season not in queue:
        set_kv(db, QUEUE_KEY, [*queue, season][-10:])


def backfill(
    client: JolpicaClient, season: int, now: datetime | None = None, limit: int = 30
) -> int:
    """Fetch standings for completed rounds that have no snapshot, at most `limit` requests.
    Returns the number of requests made. Stops (keeping what it has) when the API fails;
    the next run carries on."""
    now = now or utcnow()
    with session_scope() as db:
        missing = missing_rounds(db, season, now)
    past = season < now.year
    made = 0
    for kind in KINDS:
        for rnd in missing[kind]:
            if made >= limit:
                return made
            made += 1
            try:
                lst = client.standings(
                    kind, season, rnd, max_age=None if past else timedelta(hours=6)
                )
            except SourceUnavailable:
                log.warning(
                    "standings unavailable; progression backfill paused",
                    extra={"season": season, "round": rnd},
                )
                return made
            if lst is None or int(lst.get("round", -1)) != rnd:
                continue  # not published (yet): leave the gap rather than guess
            with session_scope() as db:
                apply_standings(db, kind, lst, now=now)
    return made


def pending_seasons(db: DB, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    queued = [int(s) for s in (get_kv(db, QUEUE_KEY) or [])]
    return [now.year, *[s for s in queued if s != now.year]]


def mark_done(db: DB, season: int, now: datetime | None = None) -> None:
    if any(missing_rounds(db, season, now).values()):
        return
    queue = [s for s in (get_kv(db, QUEUE_KEY) or []) if s != season]
    set_kv(db, QUEUE_KEY, queue)
