"""Per-session weather from Open-Meteo, stored in session_weather.

`sync()` refreshes forecasts for sessions in the next 16 days (one request per event). Rain
risk is flagged when the chance of rain during qualifying or the race reaches 40%."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import session_scope, utcnow
from pitwall.models import SESSION_LABELS, Event, Session, SessionWeather
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.openmeteo import FORECAST_DAYS, OpenMeteoClient, describe

log = logging.getLogger(__name__)

RAIN_RISK = 40  # percent
RAIN_FLAG_SESSIONS = ("Q", "RACE")


@dataclass
class Forecast:
    temp_c: float | None
    rain_chance: int | None
    precip_mm: float | None
    wind_kmh: float | None
    weather_code: int | None


def summarize(hourly: dict[str, Any], start: datetime, end: datetime) -> Forecast | None:
    """Aggregate the hourly values covering [start, end]: mean temperature, max rain chance,
    total precipitation, max wind and the most severe weather code."""
    times = hourly.get("time") or []
    first = start.replace(minute=0, second=0, microsecond=0)
    idx = []
    for i, t in enumerate(times):
        try:
            when = datetime.fromisoformat(t).replace(tzinfo=UTC)
        except ValueError:
            continue
        if first <= when < end:
            idx.append(i)
    if not idx:
        return None

    def vals(key: str) -> list[float]:
        series = hourly.get(key) or []
        return [series[i] for i in idx if i < len(series) and series[i] is not None]

    temps, rain = vals("temperature_2m"), vals("precipitation_probability")
    precip, wind, codes = vals("precipitation"), vals("wind_speed_10m"), vals("weather_code")
    return Forecast(
        temp_c=round(sum(temps) / len(temps), 1) if temps else None,
        rain_chance=int(max(rain)) if rain else None,
        precip_mm=round(sum(precip), 1) if precip else None,
        wind_kmh=round(max(wind)) if wind else None,
        weather_code=int(max(codes)) if codes else None,
    )


def _targets(now: datetime) -> list[tuple[int, float, float, list[tuple[int, datetime, datetime]]]]:
    horizon = now + timedelta(days=FORECAST_DAYS - 1)
    with session_scope() as db:
        events: dict[int, tuple[float, float, list[tuple[int, datetime, datetime]]]] = {}
        rows = db.execute(
            select(Session, Event)
            .join(Event)
            .where(
                Session.start_utc > now - timedelta(hours=3),
                Session.start_utc < horizon,
                Session.status == "scheduled",
                Event.status == "scheduled",
            )
        ).all()
        for sess, ev in rows:
            c = ev.circuit
            if c.lat is None or c.lng is None or sess.start_utc is None:
                continue
            end = sess.end_utc_est or sess.start_utc + timedelta(hours=1)
            events.setdefault(ev.id, (c.lat, c.lng, []))[2].append((sess.id, sess.start_utc, end))
    return [(eid, lat, lng, sessions) for eid, (lat, lng, sessions) in events.items()]


def sync(client: OpenMeteoClient, now: datetime | None = None, event_id: int | None = None) -> int:
    """Refresh forecasts. Returns the number of sessions updated."""
    now = now or utcnow()
    updated = 0
    for eid, lat, lng, sessions in _targets(now):
        if event_id is not None and eid != event_id:
            continue
        try:
            hourly = client.hourly(lat, lng)
        except SourceUnavailable:
            log.warning("weather unavailable; keeping previous forecasts")
            return updated
        with session_scope() as db:
            for sid, start, end in sessions:
                fc = summarize(hourly, start, end)
                if fc is None:
                    continue
                row = db.get(SessionWeather, sid) or SessionWeather(session_id=sid)
                row.fetched_at = now
                row.temp_c, row.rain_chance, row.precip_mm = fc.temp_c, fc.rain_chance, fc.precip_mm
                row.wind_kmh, row.weather_code = fc.wind_kmh, fc.weather_code
                db.add(row)
                updated += 1
    return updated


@dataclass
class WeatherView:
    icon: str
    summary: str
    temp: str
    rain: str
    rain_chance: int | None
    wind: str
    risk: bool  # rain risk on quali or race

    @property
    def short(self) -> str:
        bits = [b for b in (f"{self.icon} {self.temp}".strip(), self.rain) if b]
        return " · ".join(bits)


def view(row: SessionWeather | None, session_type: str) -> WeatherView | None:
    if row is None:
        return None
    icon, summary = describe(row.weather_code)
    temp = ""
    if row.temp_c is not None:
        temp = f"{round(row.temp_c)}°C / {round(row.temp_c * 9 / 5 + 32)}°F"
    rain = f"{row.rain_chance}% rain" if row.rain_chance is not None else ""
    wind = f"{row.wind_kmh:g} km/h wind" if row.wind_kmh is not None else ""
    risk = (
        session_type in RAIN_FLAG_SESSIONS
        and row.rain_chance is not None
        and row.rain_chance >= RAIN_RISK
    )
    return WeatherView(icon, summary, temp, rain, row.rain_chance, wind, risk)


def for_event(db: DB, event: Event) -> dict[int, WeatherView]:
    ids = {s.id: s.type for s in event.sessions}
    rows = db.scalars(select(SessionWeather).where(SessionWeather.session_id.in_(ids)))
    out = {}
    for row in rows:
        v = view(row, ids[row.session_id])
        if v:
            out[row.session_id] = v
    return out


def rain_warnings(event: Event, views: dict[int, WeatherView]) -> list[str]:
    out = []
    for s in sorted(event.sessions, key=lambda s: s.start_utc or datetime.max.replace(tzinfo=UTC)):
        v = views.get(s.id)
        if v and v.risk:
            out.append(f"Rain risk {v.rain_chance}% for {SESSION_LABELS.get(s.type, s.type)}")
    return out
