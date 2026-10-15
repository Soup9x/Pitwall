"""Fetches OpenF1 per-race detail (best lap, stints, pit stops) for followed drivers.

Network calls happen outside DB transactions; each stored row is committed on its own so a
partial run keeps what it fetched. Sessions with no OpenF1 data are stored with empty values
so they aren't requested again."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from pitwall.db import session_scope, utcnow
from pitwall.models import DriverSessionDetail, Event, FollowedDriver, Result, Session
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.openf1 import (
    FIRST_SEASON,
    SESSION_NAMES,
    OpenF1Client,
    match_session,
    summarize,
)

log = logging.getLogger(__name__)
SETTLE = timedelta(hours=1)  # only ask for data well after the session ended


@dataclass(frozen=True)
class Target:
    session_id: int
    season: int
    stype: str
    start_utc: datetime
    driver_id: str
    driver_number: int


def pending_targets(now: datetime, season: int | None = None) -> list[Target]:
    with session_scope() as db:
        followed = {f.driver_id for f in db.scalars(select(FollowedDriver))}
        if not followed:
            return []
        q = (
            select(Result, Session, Event)
            .join(Session, Result.session_id == Session.id)
            .join(Event, Session.event_id == Event.id)
            .where(
                Result.driver_id.in_(followed),
                Session.type.in_(tuple(SESSION_NAMES)),
                Event.season >= FIRST_SEASON,
                Session.start_utc.is_not(None),
                Session.end_utc_est < now - SETTLE,
                Result.number.is_not(None),
            )
        )
        if season is not None:
            q = q.where(Event.season == season)
        have = {(d.session_id, d.driver_id) for d in db.scalars(select(DriverSessionDetail))}
        out = []
        for res, sess, ev in db.execute(q).all():
            if (sess.id, res.driver_id) in have:
                continue
            out.append(
                Target(sess.id, ev.season, sess.type, sess.start_utc, res.driver_id, res.number)
            )
    return sorted(out, key=lambda t: t.start_utc)


def sync(
    client: OpenF1Client, now: datetime | None = None, season: int | None = None, limit: int = 200
) -> int:
    """Fetch details for up to `limit` (session, driver) pairs. Returns rows stored."""
    now = now or utcnow()
    targets = pending_targets(now, season)[:limit]
    session_lists: dict[tuple[int, str], list] = {}
    stored = 0
    for t in targets:
        name = SESSION_NAMES[t.stype]
        try:
            if (t.season, name) not in session_lists:
                session_lists[(t.season, name)] = client.sessions(t.season, name)
            key = match_session(session_lists[(t.season, name)], t.start_utc)
            if key is None:
                laps, stints, pits = [], [], []
            else:
                laps = client.laps(key, t.driver_number)
                stints = client.stints(key, t.driver_number)
                pits = client.pits(key, t.driver_number)
        except SourceUnavailable:
            log.warning("OpenF1 unavailable; will retry later")
            break
        best, pit_count, stint_list = summarize(laps, stints, pits)
        with session_scope() as db:
            db.add(
                DriverSessionDetail(
                    session_id=t.session_id,
                    driver_id=t.driver_id,
                    openf1_session_key=key,
                    best_lap_s=best,
                    pit_count=pit_count if key is not None and laps else None,
                    stints_json=json.dumps(stint_list),
                    fetched_at=now,
                )
            )
        stored += 1
    if stored:
        log.info("OpenF1 details stored", extra={"rows": stored})
    return stored
