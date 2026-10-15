"""HTTP routes: public (health, ICS, login) and authenticated UI pages."""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from pitwall import __version__, jobs, scheduler
from pitwall.config import get_env
from pitwall.db import get_db, session_scope, utcnow
from pitwall.models import (
    SESSION_LABELS,
    SESSION_TYPES,
    Driver,
    Event,
    FollowedDriver,
    NotificationLog,
    Outbox,
    Result,
    SourceStatus,
    StandingsSnapshot,
)
from pitwall.notify import builders, outbox
from pitwall.notify.dispatch import channel_urls, mask_url
from pitwall.notify.dispatch import notify as dispatch_notify
from pitwall.services import ics, reminders, standings, track_stats
from pitwall.services.presenters import (
    current_or_next_event,
    event_sessions,
    last_completed_event,
    next_session,
    session_view,
)
from pitwall.settings_store import (
    ALERT_LABELS,
    ALERT_TYPES,
    CHANNELS,
    get_kv,
    load_settings,
    parse_hhmm,
    save_settings,
)
from pitwall.templating import get_jinja_env
from pitwall.web.auth import (
    check_password,
    checked_form,
    client_key,
    csrf_token,
    is_logged_in,
    limiter,
    require_user,
    safe_next,
)

log = logging.getLogger(__name__)
templates = Jinja2Templates(env=get_jinja_env())

public = APIRouter()
private = APIRouter(dependencies=[Depends(require_user)])

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


def page(request: Request, name: str, db: DB, **ctx: Any) -> HTMLResponse:
    settings = load_settings(db)
    base = {
        "settings": settings,
        "csrf": csrf_token(request),
        "path": request.url.path,
        "version": __version__,
        "flash": request.session.pop("flash", None),
        "backfill": get_kv(db, "backfill_progress"),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def back(to: str) -> RedirectResponse:
    return RedirectResponse(to, status_code=303)


# ---------------------------------------------------------------------------- public


@public.get("/health")
def health() -> JSONResponse:
    body: dict[str, Any] = {"status": "ok", "version": __version__, "time": utcnow().isoformat()}
    code = 200
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
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
        body["db"] = f"error: {type(exc).__name__}"
        code = 503
    body["scheduler"] = scheduler.state()
    return JSONResponse(body, status_code=code)


@public.get("/calendar.ics")
def calendar_feed(
    token: str = Query(default=""),
    sessions: str | None = Query(default=None),
    db: DB = Depends(get_db),
) -> Response:
    expected = get_env().ics_token
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=404)
    body = ics.build_calendar(db, load_settings(db), ics.parse_filter(sessions))
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="pitwall.ics"',
            "Cache-Control": "no-cache",
        },
    )


@public.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    if is_logged_in(request):
        return back(safe_next(next))  # type: ignore[return-value]
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf": csrf_token(request),
            "next": safe_next(next),
            "error": None,
            "configured": bool(get_env().admin_password),
        },
    )


@public.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    form = await checked_form(request)
    key = client_key(request)
    nxt = safe_next(str(form.get("next", "/")))
    ctx = {"next": nxt, "configured": bool(get_env().admin_password)}
    if limiter.blocked(key):
        log.warning("login rate limited", extra={"client": key})
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                **ctx,
                "csrf": csrf_token(request),
                "error": "Too many attempts. Try again in 15 minutes.",
            },
            status_code=429,
        )
    if not check_password(str(form.get("password", ""))):
        limiter.fail(key)
        log.warning("failed login", extra={"client": key})
        return templates.TemplateResponse(
            request,
            "login.html",
            {**ctx, "csrf": csrf_token(request), "error": "Wrong password."},
            status_code=401,
        )
    limiter.reset(key)
    request.session.clear()
    request.session["auth"] = True
    return back(nxt)


@public.post("/logout")
async def logout(request: Request) -> Response:
    await checked_form(request)
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------------------- dashboard


def _spoiler_hidden(db: DB, event: Event | None) -> bool:
    settings = load_settings(db)
    return bool(
        settings.spoiler_enabled and event is not None and not outbox.is_watched(db, event.id)
    )


@private.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DB = Depends(get_db)) -> HTMLResponse:
    settings = load_settings(db)
    now = utcnow()
    nxt = next_session(db, now)
    event = current_or_next_event(db, now)
    last_event = last_completed_event(db, now)
    season = standings.latest_season_with_standings(db)
    drivers = standings.table(db, season, "driver") if season else None
    constructors = standings.table(db, season, "constructor") if season else None
    followed = list(db.scalars(select(FollowedDriver)))
    form = builders._form(db, settings, now) if followed else []
    return page(
        request,
        "dashboard.html",
        db,
        next_session=session_view(nxt, settings, now) if nxt else None,
        next_event=nxt.event if nxt else None,
        event=event,
        sessions=event_sessions(event, settings, now) if event else [],
        drivers=drivers,
        constructors=constructors,
        followed_rows=[r for r in (drivers.rows if drivers else []) if r.followed],
        form=form,
        spoiler_hidden=_spoiler_hidden(db, last_event),
        last_event=last_event,
    )


# ---------------------------------------------------------------------------- events


@private.get("/events", response_class=HTMLResponse)
def events_list(
    request: Request, season: int | None = None, db: DB = Depends(get_db)
) -> HTMLResponse:
    settings = load_settings(db)
    seasons = list(db.scalars(select(Event.season).distinct().order_by(Event.season.desc())))
    if season is None:
        this_year = utcnow().year
        season = this_year if this_year in seasons or not seasons else seasons[0]
    evs = list(db.scalars(select(Event).where(Event.season == season).order_by(Event.round)))
    rows = []
    for ev in evs:
        race = next((s for s in ev.sessions if s.type == "RACE"), None)
        rows.append({"event": ev, "race": session_view(race, settings) if race else None})
    return page(request, "events.html", db, rows=rows, season=season, seasons=seasons, now=utcnow())


@private.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: int, db: DB = Depends(get_db)) -> HTMLResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    settings = load_settings(db)
    results: list[dict[str, Any]] = []
    for sess in sorted(event.sessions, key=lambda s: s.start_utc or utcnow()):
        rows = list(
            db.scalars(
                select(Result)
                .where(Result.session_id == sess.id)
                .order_by(Result.position.is_(None), Result.position)
            )
        )
        if rows:
            results.append({"session": sess, "label": SESSION_LABELS[sess.type], "rows": rows})
    followed = {f.driver_id for f in db.scalars(select(FollowedDriver))}
    return page(
        request,
        "event.html",
        db,
        event=event,
        sessions=event_sessions(event, settings),
        stats=track_stats.compute(db, event),
        results=results,
        followed=followed,
        watched=outbox.is_watched(db, event.id),
        spoiler_hidden=_spoiler_hidden(db, event) and bool(results),
    )


@private.post("/events/{event_id}/watched")
async def toggle_watched(request: Request, event_id: int, db: DB = Depends(get_db)) -> Response:
    form = await checked_form(request)
    if db.get(Event, event_id) is None:
        raise HTTPException(404)
    watched = form.get("watched", "1") == "1"
    outbox.mark_watched(db, event_id, watched)
    db.commit()
    if watched:
        await run_in_threadpool(jobs.flush_job)
        flash(request, "Marked as watched — any held results/standings alerts are on their way.")
    return back(f"/events/{event_id}")


@private.get("/events/{event_id}/preview", response_class=HTMLResponse)
def preview_page(event_id: int, db: DB = Depends(get_db)) -> HTMLResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    msg = builders.preview(db, event, load_settings(db))
    return HTMLResponse(msg.html or "")


@private.post("/events/{event_id}/send-preview")
async def send_preview(request: Request, event_id: int, db: DB = Depends(get_db)) -> Response:
    await checked_form(request)
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    settings = load_settings(db)
    msg = builders.preview(db, event, settings)
    msg.dedup_content = f"manual-preview:{event.slug}:{utcnow().isoformat()}"
    row = outbox.enqueue(db, msg, settings=settings)
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
    request: Request, season: int | None = None, db: DB = Depends(get_db)
) -> HTMLResponse:
    seasons = list(
        db.scalars(
            select(StandingsSnapshot.season).distinct().order_by(StandingsSnapshot.season.desc())
        )
    )
    season = season or (seasons[0] if seasons else None)
    drivers = standings.table(db, season, "driver") if season else None
    constructors = standings.table(db, season, "constructor") if season else None
    return page(
        request,
        "standings.html",
        db,
        season=season,
        seasons=seasons,
        drivers=drivers,
        constructors=constructors,
        spoiler_hidden=_spoiler_hidden(db, last_completed_event(db)),
    )


# ---------------------------------------------------------------------------- history


@private.get("/history", response_class=HTMLResponse)
def history(request: Request, db: DB = Depends(get_db)) -> HTMLResponse:
    logs = list(
        db.scalars(select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(200))
    )
    held = list(
        db.scalars(select(Outbox).where(Outbox.status == "pending").order_by(Outbox.created_at))
    )
    return page(request, "history.html", db, logs=logs, held=held)


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


def _mask_token(token: str) -> str:
    if not token:
        return ""
    return token[:4] + "…" + token[-2:] if len(token) > 8 else "…"


@private.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DB = Depends(get_db)) -> HTMLResponse:
    settings = load_settings(db)
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
    followed = list(db.scalars(select(FollowedDriver)))
    zones = sorted(available_timezones())
    return page(
        request,
        "settings.html",
        db,
        zones=zones,
        common_zones=COMMON_ZONES,
        session_types=SESSION_TYPES,
        session_labels=SESSION_LABELS,
        offsets={t: fmt_offsets(settings.reminder_offsets.get(t, [])) for t in SESSION_TYPES},
        alert_types=ALERT_TYPES,
        alert_labels=ALERT_LABELS,
        channels=CHANNELS,
        channel_urls={c: [mask_url(u) for u in channel_urls(c)] for c in CHANNELS},
        ics_base=f"{env.base_url.rstrip('/')}/calendar.ics",
        ics_token_masked=_mask_token(env.ics_token),
        season_drivers=season_drivers,
        followed=followed,
        error=request.session.pop("settings_error", None),
    )


@private.post("/settings")
async def save_settings_route(request: Request, db: DB = Depends(get_db)) -> Response:
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
        start_year = int(str(form.get("backfill_start_year", "2000")))
        if not 1950 <= start_year <= utcnow().year:
            raise ValueError("Backfill start year must be between 1950 and this year")
        if not 0 <= auto_release <= 24 * 14:
            raise ValueError("Auto-release must be between 0 and 336 hours")
    except ValueError as exc:
        request.session["settings_error"] = str(exc)
        return back("/settings")

    old = load_settings(db)
    new = save_settings(
        db,
        {
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
            "backfill_start_year": start_year,
        },
    )
    db.commit()
    sched = scheduler.get_scheduler()
    if sched is not None:
        if new.timezone != old.timezone:
            scheduler.install_recurring_jobs(sched)
        reminders.reconcile(sched, db, new)
    flash(request, "Settings saved.")
    return back("/settings")


@private.post("/settings/followed")
async def followed_drivers(request: Request, db: DB = Depends(get_db)) -> Response:
    form = await checked_form(request)
    action = form.get("action")
    driver_id = str(form.get("driver_id", ""))
    if action == "add" and db.get(Driver, driver_id) and not db.get(FollowedDriver, driver_id):
        db.add(FollowedDriver(driver_id=driver_id))
    elif action == "remove":
        row = db.get(FollowedDriver, driver_id)
        if row:
            db.delete(row)
    db.commit()
    return back("/settings#followed")


@private.post("/settings/test/{channel}")
async def test_channel(request: Request, channel: str, db: DB = Depends(get_db)) -> Response:
    await checked_form(request)
    if channel not in CHANNELS:
        raise HTTPException(404)
    msg = builders.test_message(channel)
    ok, detail = await run_in_threadpool(dispatch_notify, channel, msg)
    db.add(
        NotificationLog(
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


@private.post("/admin/run/{job}")
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
