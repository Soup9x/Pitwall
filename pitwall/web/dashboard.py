"""Dashboard view model: next-session hero, weekend timeline, timing tower, followed drivers.

Race weekend mode runs from Thursday 00:00 (track-local) of race week until the race ends;
the dashboard then leads with the timeline, penalties, weather and tyres."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Event, FollowedDriver, User
from pitwall.notify import builders
from pitwall.services import penalties, session_data, spoilers, standings, tyres, weather
from pitwall.services.presenters import (
    SessionView,
    current_or_next_event,
    event_sessions,
    next_session,
    session_view,
)
from pitwall.services.standings import StandingRow
from pitwall.settings_store import AppSettings
from pitwall.timeutil import safe_zone

SHORT = {
    "FP1": "FP1",
    "FP2": "FP2",
    "FP3": "FP3",
    "SQ": "Sprint Quali",
    "SPRINT": "Sprint",
    "Q": "Quali",
    "RACE": "Race",
}


@dataclass
class TimelineItem:
    view: SessionView
    state: str  # done / live / next / later / cancelled

    @property
    def short(self) -> str:
        return SHORT.get(self.view.type, self.view.label)

    @property
    def marker(self) -> str:
        return {"done": "✓", "live": "●", "next": "▶", "later": "", "cancelled": "✕"}[self.state]

    @property
    def state_label(self) -> str:
        return {
            "done": "Done",
            "live": "Live now",
            "next": "Next",
            "later": "",
            "cancelled": "Cancelled",
        }[self.state]


def timeline(event: Event, settings: AppSettings, now: datetime) -> list[TimelineItem]:
    items, next_marked = [], False
    for v in event_sessions(event, settings, now):
        if v.status == "cancelled":
            state = "cancelled"
        elif v.is_past:
            state = "done"
        elif v.start_utc and v.start_utc <= now:
            state = "live"
        elif not next_marked:
            state, next_marked = "next", True
        else:
            state = "later"
        if state == "live":
            next_marked = True
        items.append(TimelineItem(v, state))
    return items


def race_weekend(event: Event | None, now: datetime) -> bool:
    """Thursday 00:00 track-local of race week until the race's estimated end."""
    if event is None:
        return False
    race = next((s for s in event.sessions if s.type == "RACE" and s.start_utc), None)
    if race is None:
        return False
    tz = safe_zone(event.circuit.tz_name) or ZoneInfo("UTC")
    race_day = race.start_utc.astimezone(tz).date()
    thursday = race_day - timedelta(days=(race_day.weekday() - 3) % 7)
    start = datetime.combine(thursday, datetime.min.time(), tzinfo=tz)
    end = race.end_utc_est or race.start_utc + timedelta(hours=3)
    return start <= now <= end


@dataclass
class FollowedCard:
    driver_id: str
    name: str
    code: str | None
    row: StandingRow | None
    form: builders.FormLine | None
    licence: int = 0


@dataclass
class Hero:
    session: SessionView
    event: Event
    forecast: Any
    tyres: tyres.TyreView
    last_year: session_data.LastYear | None
    grid: list = field(default_factory=list)
    licence: list = field(default_factory=list)


def build(
    db: DB, user: User, settings: AppSettings, now: datetime, reveal: bool = False
) -> dict[str, Any]:
    held = None if reveal else spoilers.held_event(db, user.id, settings, now)
    cut = None if reveal else spoilers.cutoff(db, user.id, settings, now)
    nxt = next_session(db, now)
    event = current_or_next_event(db, now)
    hero = None
    if nxt is not None:
        ev = nxt.event
        hero = Hero(
            session=session_view(nxt, settings, now),
            event=ev,
            forecast=weather.for_event(db, ev, settings.temp_unit).get(nxt.id),
            tyres=tyres.view(db, ev),
            last_year=session_data.last_year_conditions(db, ev),
            grid=penalties.grid_penalties(db, ev, cut),
            licence=[
                p
                for p in penalties.licence_table(db, now.date(), cut)
                if p.state in ("danger", "ban")
            ],
        )
    season = standings.latest_season_with_standings(db)
    drivers = constructors = None
    if held is None and season:
        drivers = standings.table(db, season, "driver", user_id=user.id)
        constructors = standings.table(db, season, "constructor", user_id=user.id)

    followed_ids = [
        f.driver_id
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user.id))
    ]
    cards = []
    if followed_ids:
        form = {} if held else {f.driver: f for f in builders._form(db, settings, now, user.id)}
        rows = {r.entity_id: r for r in (drivers.rows if drivers else [])}
        points = {p.driver.id: p.total for p in penalties.licence_table(db, now.date(), cut)}
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user.id)):
            d = f.driver
            cards.append(
                FollowedCard(
                    d.id,
                    d.full_name,
                    d.code,
                    rows.get(d.id),
                    form.get(d.full_name),
                    points.get(d.id, 0),
                )
            )
    return {
        "hero": hero,
        "event": event,
        "timeline": timeline(event, settings, now) if event else [],
        "weekend": race_weekend(event, now),
        "weather": weather.for_event(db, event, settings.temp_unit) if event else {},
        "rain": weather.rain_warnings(event, weather.for_event(db, event)) if event else [],
        "last_year": session_data.last_year_conditions(db, event) if event else None,
        "event_tyres": tyres.view(db, event) if event else None,
        "event_grid": penalties.grid_penalties(db, event, cut) if event else [],
        "drivers": drivers,
        "constructors": constructors,
        "followed_cards": cards,
        "held_event": held,
        "has_standings": season is not None,
        "describe_grid": penalties.describe_grid,
    }
