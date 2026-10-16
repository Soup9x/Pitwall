"""Kiosk: a read-only wall display at /kiosk?token=..., no login and no links into the app.

The token is per user (Account page) and the display always follows that user's spoiler state:
it's built from the same view model as their dashboard, never with "show anyway". A missing or
wrong token is a plain 404. The page polls /kiosk/data every 60 s; all text is inserted with
textContent by kiosk.js, so nothing in the data is ever treated as HTML."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall import __version__
from pitwall.db import get_db, utcnow
from pitwall.models import User
from pitwall.services import penalties, trivia
from pitwall.services.standings import StandingsTable
from pitwall.settings_store import load_settings
from pitwall.web import dashboard
from pitwall.web.common import templates
from pitwall.web.racedata import script_json

router = APIRouter()
# Cache-Control: no-store comes from the security middleware, as for every page.
NO_STORE = {"X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}


def owner_for(db: DB, token: str) -> User:
    user = (
        db.scalars(select(User).where(User.kiosk_token == token, User.active)).first()
        if len(token) >= 16
        else None
    )
    if user is None:
        raise HTTPException(status_code=404)
    return user


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _standings(table: StandingsTable | None, limit: int) -> list[dict[str, Any]]:
    if table is None:
        return []
    return [
        {
            "pos": r.position,
            "code": r.code or r.name.split(" ")[-1][:3].upper(),
            "name": r.name,
            "team": r.team,
            "color": r.team_color,
            "points": r.points,
            "gap": r.gap_leader,
            "change": r.change,
            "followed": r.followed,
        }
        for r in table.rows[:limit]
    ]


def payload(db: DB, owner: User, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    settings = load_settings(db, owner.id)
    d = dashboard.build(db, owner, settings, now)
    held = d["held_event"]
    hero = d["hero"]
    event = d["event"]
    out: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "tz": settings.timezone,
        "weekend": bool(d["weekend"]),
        "held": held.name if held is not None else None,
        "next": None,
        "event": None,
        "timeline": [],
        "weather": [],
        "tyres": None,
        "penalties": [],
        "drivers": _standings(d["drivers"], 10),
        "constructors": _standings(d["constructors"], 5),
        "round": d["drivers"].round if d["drivers"] else None,
        "trivia": [],
    }
    if hero is not None:
        s = hero.session
        out["next"] = {
            "event": hero.event.name,
            "session": s.label,
            "start": _iso(s.start_utc),
            "end": _iso(s.end_utc),
            "when": s.owner_time,
            "track": s.local_time,
            "forecast": hero.forecast.short if hero.forecast else None,
        }
    if event is not None:
        out["event"] = {
            "name": event.name,
            "season": event.season,
            "round": event.round,
            "place": ", ".join(p for p in (event.circuit.locality, event.circuit.country) if p),
            "sprint": event.is_sprint_weekend,
        }
        wx = d["weather"]
        for item in d["timeline"]:
            v = item.view
            out["timeline"].append(
                {
                    "label": item.short,
                    "state": item.state,
                    "marker": item.marker,
                    "when": v.owner_time,
                    "start": _iso(v.start_utc),
                }
            )
            if v.id in wx and v.status != "cancelled":
                out["weather"].append({"label": item.short, "text": wx[v.id].short})
        nom = d["event_tyres"].nomination if d["event_tyres"] else None
        if nom is not None:
            out["tyres"] = {"hard": nom.hard, "medium": nom.medium, "soft": nom.soft}
        out["penalties"] = [
            {"driver": p.driver.full_name, "text": penalties.describe_grid(p)}
            for p in d["event_grid"]
        ]
        if held is None:  # trivia uses the latest results, so it waits for a held event
            facts = trivia.for_preview(db, event, avoid_recent=False, user_id=owner.id)
            out["trivia"] = [f.text for f in facts]
    return out


@router.get("/kiosk", response_class=HTMLResponse, include_in_schema=False)
def kiosk_page(
    request: Request,
    token: str = Query(default=""),
    layout: str = Query(default="landscape"),
    db: DB = Depends(get_db),
) -> HTMLResponse:
    owner = owner_for(db, token)
    data = payload(db, owner)
    return templates.TemplateResponse(
        request,
        "kiosk.html",
        {
            "data": script_json(data),
            "portrait": layout == "portrait",
            "version": __version__,
        },
        headers=NO_STORE,
    )


@router.get("/kiosk/data", include_in_schema=False)
def kiosk_data(token: str = Query(default=""), db: DB = Depends(get_db)) -> JSONResponse:
    return JSONResponse(payload(db, owner_for(db, token)), headers=NO_STORE)
