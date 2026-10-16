"""HTTP routes: public (health, ICS, login) and authenticated UI pages."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from pitwall import __version__, jobs, scheduler
from pitwall.config import get_env
from pitwall.db import get_db, session_scope, utcnow
from pitwall.logging_setup import register_secrets
from pitwall.models import (
    OWNER_ID,
    SESSION_LABELS,
    SESSION_TYPES,
    Circuit,
    Driver,
    Event,
    FollowedDriver,
    NewsFeed,
    NotificationLog,
    NotificationTarget,
    Outbox,
    Result,
    Session,
    SourceStatus,
    StandingsSnapshot,
    User,
)
from pitwall.notify import builders, outbox
from pitwall.notify import targets as targets_svc
from pitwall.notify.dispatch import mask_url
from pitwall.notify.dispatch import notify as dispatch_notify
from pitwall.services import (
    circuit_maps,
    driver_stats,
    exports,
    head_to_head,
    ics,
    news,
    progression,
    reminders,
    spoilers,
    standings,
    title_math,
    track_stats,
    trivia,
    weather,
)
from pitwall.services.presenters import (
    event_sessions,
    session_view,
)
from pitwall.settings_store import (
    ALERT_LABELS,
    ALERT_TYPES,
    CHANNELS,
    DIGESTIBLE,
    WEEKDAYS,
    get_kv,
    load_settings,
    parse_hhmm,
    save_settings,
)
from pitwall.units import TEMP_UNITS
from pitwall.web import dashboard as dashboard_view
from pitwall.web import racedata
from pitwall.web.auth import checked_form, require_admin, require_user, safe_next, session_user
from pitwall.web.common import back, flash, page

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

public = APIRouter()
private = APIRouter(dependencies=[Depends(require_user)])
admin = APIRouter(dependencies=[Depends(require_admin)])

COMMON_ZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Toronto",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
]


# ---------------------------------------------------------------------------- public


def _local_request(request: Request) -> bool:
    """A direct request from this machine (e.g. the Docker healthcheck), not via a proxy."""
    host = request.client.host if request.client else ""
    proxied = any(
        h in request.headers
        for h in ("x-forwarded-for", "cf-connecting-ip", "forwarded", "x-real-ip")
    )
    return host in ("127.0.0.1", "::1", "localhost") and not proxied


@public.get("/health")
def health(request: Request) -> JSONResponse:
    """Status only for the internet; details for local checks and logged-in users."""
    detailed = _local_request(request) or session_user(request) is not None
    body: dict[str, Any] = {"status": "ok"}
    code = 200
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
            if not detailed:
                return JSONResponse(body)
            body.update({"version": __version__, "time": utcnow().isoformat()})
            body["db"] = "ok"
            body["sources"] = {
                s.source: {
                    "last_success": s.last_success_at.isoformat() if s.last_success_at else None,
                    "last_error_at": s.last_error_at.isoformat() if s.last_error_at else None,
                }
                for s in db.scalars(select(SourceStatus))
            }
            progress = get_kv(db, "backfill_progress") or {}
            body["backfill"] = {
                k: progress.get(k) for k in ("running", "done", "total", "finished_at")
            }
            body["outbox_pending"] = db.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.status == "pending")
            )
    except Exception as exc:
        body["status"] = "error"
        if detailed:
            body["db"] = f"error: {type(exc).__name__}"
        code = 503
    if detailed:
        body["scheduler"] = scheduler.state()
    return JSONResponse(body, status_code=code)


@public.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Served from the root so the service worker's scope is the whole app."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")


@public.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@public.get("/calendar.ics")
def calendar_feed(
    token: str = Query(default=""),
    sessions: str | None = Query(default=None),
    db: DB = Depends(get_db),
) -> Response:
    owner = (
        db.scalars(select(User).where(User.ics_token == token, User.active)).first()
        if len(token) >= 16
        else None
    )
    if owner is None:
        raise HTTPException(status_code=404)
    body = ics.build_calendar(db, load_settings(db, owner.id), ics.parse_filter(sessions))
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="pitwall.ics"',
            "Cache-Control": "no-cache",
        },
    )


@public.get("/circuit-maps/{circuit_id}")
def circuit_map(circuit_id: str, db: DB = Depends(get_db)) -> Response:
    """Public (no login) so email clients can load the image; maps aren't sensitive."""
    circuit = db.get(Circuit, circuit_id)
    path = circuit_maps.local_file(circuit) if circuit else None
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------- dashboard


def _spoiler_hidden(db: DB, event: Event | None, user: User) -> bool:
    """Same rule as the alert queue: hidden until watched or auto-released."""
    if event is None:
        return False
    return spoilers.held(db, event.id, user.id, load_settings(db, user.id))


def _held_event(db: DB, user: User, reveal: bool = False) -> Event | None:
    """The latest event whose results are held for this user (None if nothing is held, or the
    user asked to show this page anyway). Held data is never sent to the browser at all."""
    if reveal:
        return None
    return spoilers.held_event(db, user.id, load_settings(db, user.id))


def _followed(db: DB, user: User) -> list[FollowedDriver]:
    return list(db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user.id)))


@private.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    settings = load_settings(db, user.id)
    now = utcnow()
    return page(
        request,
        "dashboard.html",
        db,
        user,
        now=now,
        **dashboard_view.build(db, user, settings, now, reveal),
        **racedata.news_context(
            db, user, settings, news.visible(db, user.id, settings, limit=6, reveal=reveal)
        ),
    )


# ---------------------------------------------------------------------------- events


@private.get("/events", response_class=HTMLResponse)
def events_list(
    request: Request,
    season: int | None = None,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    settings = load_settings(db, user.id)
    seasons = list(db.scalars(select(Event.season).distinct().order_by(Event.season.desc())))
    if season is None:
        this_year = utcnow().year
        season = this_year if this_year in seasons or not seasons else seasons[0]
    evs = list(db.scalars(select(Event).where(Event.season == season).order_by(Event.round)))
    rows = []
    for ev in evs:
        race = next((s for s in ev.sessions if s.type == "RACE"), None)
        rows.append({"event": ev, "race": session_view(race, settings) if race else None})
    return page(
        request, "events.html", db, user, rows=rows, season=season, seasons=seasons, now=utcnow()
    )


@private.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(
    request: Request,
    event_id: int,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    settings = load_settings(db, user.id)
    hidden = not reveal and _spoiler_hidden(db, event, user)
    # Trivia uses the latest results (milestones, streaks), so it waits for any held event.
    held = _held_event(db, user, reveal)
    results: list[dict[str, Any]] = []
    for sess in sorted(event.sessions, key=lambda s: s.start_utc or utcnow()):
        if hidden:
            break
        rows = list(
            db.scalars(
                select(Result)
                .where(Result.session_id == sess.id)
                .order_by(Result.position.is_(None), Result.position)
            )
        )
        if rows:
            results.append({"session": sess, "label": SESSION_LABELS[sess.type], "rows": rows})
    followed = {f.driver_id for f in _followed(db, user)}
    return page(
        request,
        "event.html",
        db,
        user,
        event=event,
        sessions=event_sessions(event, settings),
        stats=track_stats.compute(db, event),
        facts=[]
        if held is not None
        else trivia.for_preview(db, event, avoid_recent=False, user_id=user.id),
        weather=(wx := weather.for_event(db, event, settings.temp_unit)),
        rain=weather.rain_warnings(event, wx),
        map_url=circuit_maps.map_url(event.circuit),
        results=results,
        followed=followed,
        watched=outbox.is_watched(db, event.id, user.id),
        spoiler_hidden=hidden and spoilers.results_arrived_at(db, event.id) is not None,
        held_event=held,
        **racedata.event_context(db, event, user, settings, reveal),
    )


@private.post("/events/{event_id}/watched")
async def toggle_watched(
    request: Request, event_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    if db.get(Event, event_id) is None:
        raise HTTPException(404)
    watched = form.get("watched", "1") == "1"
    outbox.mark_watched(db, event_id, watched, user_id=user.id)
    db.commit()
    if watched:
        await run_in_threadpool(jobs.flush_job)
        flash(request, "Marked as watched — any held results/standings alerts are on their way.")
    return back(f"/events/{event_id}")


@private.get("/events/{event_id}/preview", response_class=HTMLResponse)
def preview_page(
    event_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> HTMLResponse:
    # Same content as the email, so the same spoiler rules apply (see builders.preview_context).
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    msg = builders.preview(db, event, load_settings(db, user.id), user_id=user.id)
    return HTMLResponse(msg.html or "")


@private.post("/events/{event_id}/send-preview")
async def send_preview(
    request: Request, event_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    settings = load_settings(db, user.id)
    now = utcnow()
    row = jobs.send_preview(
        db, event, settings, now, f"manual-preview:{event.slug}:{now}", user_id=user.id
    )
    db.commit()
    await run_in_threadpool(jobs.flush_job)
    flash(
        request,
        "Preview queued."
        if row
        else "Preview is not routed to any channel (see Settings → Routing).",
    )
    return back(f"/events/{event_id}")


# ---------------------------------------------------------------------------- standings


@private.get("/standings", response_class=HTMLResponse)
def standings_page(
    request: Request,
    season: int | None = None,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    seasons = sorted(
        set(db.scalars(select(StandingsSnapshot.season).distinct()))
        | set(
            db.scalars(
                select(Event.season)
                .join(Session, Session.event_id == Event.id)
                .where(Session.type == "RACE", Session.results_fetched_at.is_not(None))
                .distinct()
            )
        ),
        reverse=True,
    )
    season = season or (seasons[0] if seasons else None)
    held = _held_event(db, user, reveal)
    hide = held is not None and season is not None and season >= held.season
    drivers = constructors = None
    if season and not hide:
        drivers = standings.table(db, season, "driver", user_id=user.id)
        constructors = standings.table(db, season, "constructor", user_id=user.id)
    chart = None
    missing: list[int] = []
    if season:
        # Spoiler mode: the chart stops at the round before the held event.
        upto = held.round - 1 if held is not None and held.season == season else None
        if held is not None and held.season < season:
            upto = 0
        data = {
            kind: progression.build(db, season, kind, user_id=user.id, upto_round=upto)
            for kind in progression.KINDS
        }
        missing = sorted(set(data["driver"]["missing"]) | set(data["constructor"]["missing"]))
        if missing:
            progression.request(db, season)
            db.commit()
        if data["driver"]["series"] or data["constructor"]["series"]:
            chart = racedata.script_json(data)
    return page(
        request,
        "standings.html",
        db,
        user,
        season=season,
        seasons=seasons,
        drivers=drivers,
        constructors=constructors,
        title=[
            tm
            for k in ("driver", "constructor")
            if season and not hide and (tm := title_math.for_season(db, season, k))
        ],
        held_event=held if hide else None,
        progression_chart=chart,
        progression_missing=missing,
        chart_held=held if held is not None and held.season <= (season or 0) else None,
    )


def _csv_response(body: str, filename: str) -> Response:
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _refuse_if_held(db: DB, user: User, season: int | None, reveal: bool) -> None:
    held = _held_event(db, user, reveal)
    if held is not None and (season is None or season >= held.season):
        raise HTTPException(
            409,
            f"Hidden by spoiler mode until you mark {held.name} as watched "
            "(add ?reveal=1 to download anyway).",
        )


@private.get("/export/standings.csv")
def export_standings(
    season: int | None = None,
    kind: str = "driver",
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    if kind not in ("driver", "constructor"):
        raise HTTPException(404)
    season = season or standings.latest_season_with_standings(db)
    _refuse_if_held(db, user, season, reveal)
    body = exports.standings_csv(db, season, kind) if season else None
    if body is None:
        raise HTTPException(404, "No standings stored for that season")
    return _csv_response(body, f"pitwall-{kind}-standings-{season}.csv")


@private.get("/export/drivers.csv")
def export_drivers(
    season: int | None = None,
    driver: str | None = None,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    season = season or utcnow().year
    _refuse_if_held(db, user, season, reveal)
    body = exports.drivers_csv(db, season, [driver] if driver else None, user_id=user.id)
    name = driver or "followed-drivers"
    return _csv_response(body, f"pitwall-{name}-{season}.csv")


@private.get("/head-to-head", response_class=HTMLResponse)
def head_to_head_page(
    request: Request,
    season: int | None = None,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    seasons = list(
        db.scalars(
            select(Event.season)
            .join(Session)
            .join(Result, Result.session_id == Session.id)
            .distinct()
            .order_by(Event.season.desc())
        )
    )
    season = season or (seasons[0] if seasons else None)
    held = _held_event(db, user, reveal)
    hide = held is not None and season is not None and season >= held.season
    return page(
        request,
        "head_to_head.html",
        db,
        user,
        season=season,
        seasons=seasons,
        pairs=head_to_head.season(db, season) if season and not hide else [],
        held_event=held if hide else None,
    )


@private.get("/drivers/{driver_id}", response_class=HTMLResponse)
def driver_page(
    request: Request,
    driver_id: str,
    season: int | None = None,
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    driver = db.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(404)
    seasons = driver_stats.seasons_for(db, driver_id)
    season = season or (seasons[0] if seasons else None)
    if season and season not in seasons:
        seasons = sorted({*seasons, season}, reverse=True)
    held = _held_event(db, user, reveal)
    hide = held is not None and season is not None and season >= held.season
    data = driver_stats.season(db, driver_id, season) if season and not hide else None
    chart = ""
    if data and data.rows:
        chart = driver_stats.trend_svg(
            [(driver.full_name, [r.cumulative for r in data.rows])],
            [r.event.circuit.locality or r.event.short_name for r in data.rows],
        )
    return page(
        request,
        "driver.html",
        db,
        user,
        driver=driver,
        season=season,
        seasons=seasons,
        data=data,
        chart=chart,
        followed=db.get(FollowedDriver, (user.id, driver_id)) is not None,
        held_event=held if hide else None,
    )


# ---------------------------------------------------------------------------- history


@private.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    all: int = 0,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    show_all = bool(all and user.is_admin)
    logs_q = select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(200)
    held_q = select(Outbox).where(Outbox.status == "pending").order_by(Outbox.created_at)
    if not show_all:
        logs_q = logs_q.where(NotificationLog.user_id == user.id)
        held_q = held_q.where(Outbox.user_id == user.id)
    logs = list(db.scalars(logs_q))
    held = list(db.scalars(held_q))
    return page(request, "history.html", db, user, logs=logs, held=held, show_all=show_all)


# ---------------------------------------------------------------------------- settings

_OFFSET_RE = re.compile(r"^(\d+)\s*([dhm]?)$")


def parse_offsets(raw: str) -> list[int]:
    """'24h, 1h' -> [1440, 60]. Bare numbers are minutes."""
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip().lower()
        if not part:
            continue
        m = _OFFSET_RE.match(part)
        if not m:
            raise ValueError(f"Can't read reminder offset '{part}' (try 24h, 90m or 1d)")
        n, unit = int(m.group(1)), m.group(2) or "m"
        minutes = n * {"d": 1440, "h": 60, "m": 1}[unit]
        if not 1 <= minutes <= 14 * 1440:
            raise ValueError("Reminder offsets must be between 1 minute and 14 days")
        out.append(minutes)
    return sorted(set(out), reverse=True)


def fmt_offsets(minutes: list[int]) -> str:
    def one(m: int) -> str:
        if m % 1440 == 0:
            return f"{m // 1440}d"
        if m % 60 == 0:
            return f"{m // 60}h"
        return f"{m}m"

    return ", ".join(one(m) for m in minutes)


@private.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> HTMLResponse:
    settings = load_settings(db, user.id)
    env = get_env()
    season_drivers = list(
        db.scalars(
            select(Driver)
            .join(StandingsSnapshot, StandingsSnapshot.entity_id == Driver.id)
            .where(
                StandingsSnapshot.type == "driver",
                StandingsSnapshot.season
                == func.coalesce(select(func.max(StandingsSnapshot.season)).scalar_subquery(), 0),
            )
            .distinct()
            .order_by(Driver.family_name)
        )
    )
    if not season_drivers:
        season_drivers = list(db.scalars(select(Driver).order_by(Driver.family_name).limit(400)))
    followed = _followed(db, user)
    zones = sorted(available_timezones())
    return page(
        request,
        "settings.html",
        db,
        user,
        zones=zones,
        common_zones=COMMON_ZONES,
        session_types=SESSION_TYPES,
        session_labels=SESSION_LABELS,
        offsets={t: fmt_offsets(settings.reminder_offsets.get(t, [])) for t in SESSION_TYPES},
        alert_types=ALERT_TYPES,
        digestible=DIGESTIBLE,
        weekdays=WEEKDAYS,
        alert_labels=ALERT_LABELS,
        channels=CHANNELS,
        targets={c: targets_svc.db_targets(db, c, user.id) for c in CHANNELS},
        env_targets={
            c: [mask_url(u) for u in targets_svc.env_urls(c)] if user.id == OWNER_ID else []
            for c in CHANNELS
        },
        target_kinds=targets_svc.KINDS,
        mask_url=mask_url,
        ics_url=f"{env.base_url.rstrip('/')}/calendar.ics?token={user.ics_token}"
        if user.ics_token
        else None,
        season_drivers=season_drivers,
        followed=followed,
        error=request.session.pop("settings_error", None),
        channel_form=request.session.pop("channel_form", None) or {},
        smtp_security=targets_svc.SMTP_SECURITY,
        news_feeds=list(db.scalars(select(NewsFeed).order_by(NewsFeed.name)))
        if user.is_admin
        else [],
        track_keywords=news.track_keywords(db) if user.is_admin else [],
    )


@private.post("/settings")
async def save_settings_route(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    try:
        tz = str(form.get("timezone", "")).strip()
        if tz not in available_timezones():
            raise ValueError(f"Unknown timezone '{tz}'")
        offsets = {t: parse_offsets(str(form.get(f"offset_{t}", ""))) for t in SESSION_TYPES}
        routing = {a: [c for c in CHANNELS if form.get(f"route_{a}_{c}")] for a in ALERT_TYPES}
        for key in ("quiet_start", "quiet_end", "odd_start", "odd_end"):
            parse_hhmm(str(form.get(key, "00:00")))
        auto_release = int(str(form.get("spoiler_auto_release_hours", "0")) or 0)
        start_year = None
        if user.is_admin and form.get("backfill_start_year"):
            start_year = int(str(form.get("backfill_start_year")))
            if not 1950 <= start_year <= utcnow().year:
                raise ValueError("Backfill start year must be between 1950 and this year")
        if not 0 <= auto_release <= 24 * 14:
            raise ValueError("Auto-release must be between 0 and 336 hours")
        digest_day = str(form.get("digest_day", "mon"))
        if digest_day not in WEEKDAYS:
            raise ValueError("Pick a day for the weekly digest")
        digest_time = str(form.get("digest_time", "08:00"))
        parse_hhmm(digest_time)
        digest_types = [t for t in DIGESTIBLE if form.get(f"digest_type_{t}")]
        temp_unit = str(form.get("temp_unit", "both"))
        if temp_unit not in TEMP_UNITS:
            raise ValueError("Pick °C, °F or both for temperatures")
    except ValueError as exc:
        request.session["settings_error"] = str(exc)
        return back("/settings")

    updates: dict[str, Any] = {
        "timezone": tz,
        "reminder_offsets": offsets,
        "routing": routing,
        "quiet_enabled": bool(form.get("quiet_enabled")),
        "quiet_start": str(form.get("quiet_start")),
        "quiet_end": str(form.get("quiet_end")),
        "spoiler_enabled": bool(form.get("spoiler_enabled")),
        "spoiler_auto_release_hours": auto_release,
        "odd_start": str(form.get("odd_start")),
        "odd_end": str(form.get("odd_end")),
        "digest_enabled": bool(form.get("digest_enabled")),
        "digest_day": digest_day,
        "digest_time": digest_time,
        "digest_types": digest_types,
        "temp_unit": temp_unit,
    }
    if start_year is not None:
        updates["backfill_start_year"] = start_year
    old = load_settings(db, user.id)
    new = save_settings(db, updates, user.id)
    db.commit()
    sched = scheduler.get_scheduler()
    if sched is not None:
        if user.id == OWNER_ID and new.timezone != old.timezone:
            scheduler.install_recurring_jobs(sched)  # nightly jobs follow the owner's timezone
        reminders.reconcile(sched, db)
    flash(request, "Settings saved.")
    return back("/settings")


THEMES = ("system", "light", "dark")


@private.post("/prefs/theme")
async def set_theme(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    theme = str(form.get("theme", ""))
    if theme in THEMES:
        save_settings(db, {"theme": theme}, user.id)
        db.commit()
    return back(safe_next(str(form.get("next", "/"))))


@private.post("/settings/followed")
async def followed_drivers(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    action = form.get("action")
    driver_id = str(form.get("driver_id", ""))
    if (
        action == "add"
        and db.get(Driver, driver_id)
        and not db.get(FollowedDriver, (user.id, driver_id))
    ):
        db.add(FollowedDriver(user_id=user.id, driver_id=driver_id))
        scheduler.run_now("openf1_sync")  # fetch their OpenF1 race detail in the background
    elif action == "remove":
        row = db.get(FollowedDriver, (user.id, driver_id))
        if row:
            db.delete(row)
    db.commit()
    return back("/settings#followed")


@private.post("/settings/targets")
async def add_target(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    channel = str(form.get("channel", ""))
    kind = str(form.get("kind", ""))
    fields: dict[str, str] = {}
    for k, v in form.multi_items():  # first non-empty value wins (fields repeat across types)
        if k != "csrf_token" and isinstance(v, str) and v.strip() and k not in fields:
            fields[k] = v
    try:
        built = targets_svc.build(channel, kind, fields)
    except targets_svc.TargetError as exc:
        # Shown next to the form (not just at the top of the page, which is scrolled away),
        # with what was typed kept - except passwords and other secrets.
        keep = (
            "smtp_host",
            "smtp_port",
            "smtp_security",
            "username",
            "from_addr",
            "to_addr",
            "address",
            "ntfy_server",
            "ntfy_topic",
            "json_url",
            "label",
        )
        request.session["channel_form"] = {
            "channel": channel,
            "kind": kind,
            "error": str(exc),
            **{k: fields[k][:200] for k in keep if k in fields},
        }
        return back(f"/settings#channel-error-{channel}")
    register_secrets(targets_svc.url_secrets(built.url))
    label = str(form.get("label", "")).strip()[:200] or built.label
    db.add(
        NotificationTarget(user_id=user.id, channel=channel, kind=kind, label=label, url=built.url)
    )
    db.commit()
    flash(
        request,
        f"Added {label}. "
        + (built.note + " " if built.note else "")
        + "Use “Test” to check it works.",
    )
    return back("/settings#channels")


def _target(db: DB, target_id: int, user: User) -> NotificationTarget:
    target = db.get(NotificationTarget, target_id)
    if target is None or target.user_id != user.id:  # users only manage their own channels
        raise HTTPException(404)
    return target


@private.post("/settings/targets/{target_id}/toggle")
async def toggle_target(
    request: Request, target_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    target = _target(db, target_id, user)
    target.enabled = not target.enabled
    db.commit()
    return back("/settings#channels")


@private.post("/settings/targets/{target_id}/delete")
async def delete_target(
    request: Request, target_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    target = _target(db, target_id, user)
    flash(request, f"Removed {target.label}.")
    db.delete(target)
    db.commit()
    return back("/settings#channels")


@private.post("/settings/targets/{target_id}/test")
async def test_target(
    request: Request, target_id: int, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    target = _target(db, target_id, user)
    msg = builders.test_message(target.channel)
    ok, detail = await run_in_threadpool(dispatch_notify, target.channel, msg, [target.url])
    db.add(
        NotificationLog(
            user_id=user.id,
            type="test",
            channel=target.channel,
            payload_hash=msg.payload_hash(),
            title=f"{msg.title} ({target.label})",
            status="sent" if ok else "failed",
            detail=detail[:1000],
        )
    )
    db.commit()
    flash(
        request,
        f"Test to {target.label}: "
        + ("sent ✅ — check that it arrived." if ok else f"failed ❌ — {detail}"),
    )
    return back("/settings#channels")


@private.post("/settings/test/{channel}")
async def test_channel(
    request: Request, channel: str, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    if channel not in CHANNELS:
        raise HTTPException(404)
    msg = builders.test_message(channel)
    msg.user_id = user.id
    ok, detail = await run_in_threadpool(dispatch_notify, channel, msg)
    db.add(
        NotificationLog(
            user_id=user.id,
            type="test",
            channel=channel,
            payload_hash=msg.payload_hash(),
            title=msg.title,
            status="sent" if ok else "failed",
            detail=detail[:1000],
        )
    )
    db.commit()
    flash(request, f"Test {channel}: {'sent ✅' if ok else 'failed ❌ — ' + detail}")
    return back("/settings#channels")


@admin.post("/admin/run/{job}")
async def run_job(request: Request, job: str) -> Response:
    await checked_form(request)
    mapping = {
        "schedule": "schedule_sync",
        "results": "results_sync",
        "backfill": "backfill_nightly",
    }
    if job not in mapping:
        raise HTTPException(404)
    if scheduler.run_now(mapping[job]):
        flash(request, f"Started {job} sync in the background.")
    else:
        flash(request, "Scheduler is not running.")
    return back("/settings#ops")
