"""Race data pages and admin forms: tyre nominations, penalty watch, race control and news."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from pitwall.db import get_db, utcnow
from pitwall.models import (
    COMPOUNDS,
    SESSION_LABELS,
    Driver,
    Event,
    FollowedDriver,
    GridPenalty,
    LicencePoints,
    NewsFeed,
    PuUsage,
    Result,
    Session,
    StandingsSnapshot,
    User,
)
from pitwall.services import news, penalties, penalty_watch, session_data, spoilers, tyres
from pitwall.services.presenters import current_or_next_event
from pitwall.settings_store import AppSettings, get_kv, load_settings, set_kv
from pitwall.units import c_to_f
from pitwall.web import dashboard
from pitwall.web.auth import checked_form, require_admin, require_user
from pitwall.web.common import back, flash, page

log = logging.getLogger(__name__)
router = APIRouter()
_EARLIEST = datetime.min.replace(tzinfo=UTC)


def _event(db: DB, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404)
    return event


def _chart_json(cond: Any, label: str, unit: str) -> str | None:
    """Track temperature series for the vendored Chart.js (read by static/charts.js)."""
    points = json.loads(cond.series_json or "[]")
    if len(points) < 2:
        return None
    to_f = unit == "F"
    return script_json(
        {
            "label": f"Track temperature, {label}",
            "unit": "°F" if to_f else "°C",
            "points": [[m, round(c_to_f(t), 1) if to_f else t] for m, t in points],
        }
    )


def script_json(data: Any) -> str:
    """JSON safe to place inside <script type="application/json">."""
    return json.dumps(data).replace("<", "\\u003c")


def event_context(
    db: DB, event: Event, user: User, settings: AppSettings, reveal: bool = False
) -> dict[str, Any]:
    """Step 2 sections of the event page: tyres, race control, measured conditions.

    Race control from competitive sessions that started after the spoiler cutoff is left out
    (not just collapsed) while the event is held."""
    cut = None if reveal else spoilers.cutoff(db, user.id, settings)
    rc_all = session_data.race_control_for(db, event.sessions)
    race_control = [rc for rc in rc_all if not spoilers.hides(cut, rc.session.start_utc, event.id)]
    conditions = session_data.conditions_for(db, event.sessions)
    chart = None
    latest_first = sorted(event.sessions, key=lambda s: s.start_utc or _EARLIEST, reverse=True)
    for sess in latest_first:
        cond = conditions.get(sess.id)
        if cond is not None:
            chart = _chart_json(cond, SESSION_LABELS.get(sess.type, sess.type), settings.temp_unit)
            if chart:
                break
    return {
        "tyres": tyres.view(db, event),
        "compounds": COMPOUNDS,
        "race_control": race_control,
        "race_control_hidden": len(rc_all) - len(race_control),
        "conditions": conditions,
        "temp_chart": chart,
        "last_year": session_data.last_year_conditions(db, event),
        "sc_history": session_data.sc_history(db, event),
        "kind": session_data.kind,
        "grid_penalties": penalties.grid_penalties(db, event, cut),
        "describe_grid": penalties.describe_grid,
        "track_news": news.track_updates(db, event, user.id, settings, reveal=reveal),
        "news_tags": news.tags_of,
        "timeline": dashboard.timeline(event, settings, utcnow()),
    }


# ---------------------------------------------------------------------------- tyres


@router.post("/admin/events/{event_id}/tyres/import")
async def import_tyres(
    request: Request, event_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    event = _event(db, event_id)
    client = _pirelli_client()
    try:
        await run_in_threadpool(
            tyres.import_from_url, db, event.id, str(form.get("url", "")), client
        )
    except tyres.InvalidNomination as exc:
        db.rollback()
        flash(request, f"Not imported: {exc}")
        return back(f"/events/{event.id}#tyres")
    finally:
        client.close()
    db.commit()
    flash(request, "Tyre nomination imported from the press release.")
    return back(f"/events/{event.id}#tyres")


def _pirelli_client() -> Any:
    import httpx

    from pitwall import USER_AGENT

    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True)


@router.post("/admin/events/{event_id}/tyres")
async def save_tyres(
    request: Request, event_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    event = _event(db, event_id)
    if form.get("action") == "clear":
        tyres.clear_manual(db, event.id)
        db.commit()
        flash(request, "Manual tyre nomination removed.")
        return back(f"/events/{event.id}#tyres")
    try:
        tyres.set_manual(
            db,
            event.id,
            str(form.get("hard", "")),
            str(form.get("medium", "")),
            str(form.get("soft", "")),
            user.id,
            str(form.get("source_url", "")),
        )
    except tyres.InvalidNomination as exc:
        db.rollback()
        flash(request, f"Tyres not saved: {exc}")
        return back(f"/events/{event.id}#tyres")
    db.commit()
    log.info("tyre nomination saved", extra={"event": event.slug, "by": user.username})
    flash(request, "Tyre nomination saved.")
    return back(f"/events/{event.id}#tyres")


# ---------------------------------------------------------------------------- penalty watch


def _season_drivers(db: DB, season: int) -> list[Driver]:
    """Drivers with a result or standings entry this season (else everyone, by name)."""
    ids = set(
        db.scalars(
            select(Result.driver_id)
            .join(Session, Result.session_id == Session.id)
            .join(Event, Session.event_id == Event.id)
            .where(Event.season == season)
        )
    ) | set(
        db.scalars(
            select(StandingsSnapshot.entity_id).where(
                StandingsSnapshot.season == season, StandingsSnapshot.type == "driver"
            )
        )
    )
    q = select(Driver).order_by(Driver.family_name)
    drivers = list(db.scalars(q.where(Driver.id.in_(ids)))) if ids else []
    return drivers or list(db.scalars(q.limit(400)))


def _current_season(db: DB) -> int:
    seasons = list(db.scalars(select(Event.season).distinct().order_by(Event.season.desc())))
    year = utcnow().year
    return year if year in seasons or not seasons else seasons[0]


@router.get("/admin/penalties", response_class=HTMLResponse)
def penalties_admin(
    request: Request,
    season: int | None = None,
    db: DB = Depends(get_db),
    user: User = Depends(require_admin),
) -> HTMLResponse:
    season = season or _current_season(db)
    events = list(db.scalars(select(Event).where(Event.season == season).order_by(Event.round)))
    nxt = current_or_next_event(db)
    return page(
        request,
        "admin_penalties.html",
        db,
        user,
        season=season,
        events=events,
        default_event=nxt.id if nxt and nxt.season == season else None,
        drivers=_season_drivers(db, season),
        allocations=penalties.allocations(db, season),
        elements=penalties.PU_ELEMENTS,
        manual=penalties.manual_entries(db),
        failed=penalties.failed_documents(db),
        fia_url=get_kv(db, penalty_watch.URL_KEY) or "",
        last_poll=get_kv(db, penalty_watch.LAST_POLL_KEY),
        today=utcnow().date().isoformat(),
        describe_grid=penalties.describe_grid,
    )


def _admin_back(season: object = None, anchor: str = "") -> Response:
    q = f"?season={season}" if season else ""
    return back(f"/admin/penalties{q}{anchor}")


def _int_or_none(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


async def _alert_manual(changes: penalty_watch.Changes) -> None:
    from pitwall import jobs

    if changes and penalty_watch.send_alerts(changes, jobs.fan_out):
        await run_in_threadpool(jobs.flush_job)


@router.post("/admin/penalties/source")
async def penalties_source(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    url = str(form.get("fia_url", "")).strip()
    if url and not url.startswith("https://www.fia.com/"):
        flash(request, "That doesn't look like an FIA documents page (https://www.fia.com/...).")
        return _admin_back(anchor="#source")
    set_kv(db, penalty_watch.URL_KEY, url or None)
    db.commit()
    flash(request, "FIA documents page saved." if url else "FIA document polling switched off.")
    return _admin_back(anchor="#source")


@router.post("/admin/penalties/poll")
async def penalties_poll_now(request: Request, user: User = Depends(require_admin)) -> Response:
    await checked_form(request)
    from pitwall import jobs

    await run_in_threadpool(jobs.penalty_watch_job, None, True)
    flash(request, "Checked the FIA documents page (see the log for details).")
    return _admin_back(anchor="#source")


@router.post("/admin/penalties/allocation")
async def penalties_allocation(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    try:
        season = int(str(form.get("season")))
        penalties.set_allocation(
            db, season, str(form.get("element", "")), _int_or_none(form.get("allowed"))
        )
    except ValueError as exc:
        db.rollback()
        flash(request, f"Not saved: {exc}")
        return _admin_back(form.get("season"), "#allocation")
    db.commit()
    flash(request, "Allocation saved.")
    return _admin_back(season, "#allocation")


@router.post("/admin/penalties/usage")
async def penalties_usage(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    try:
        event = _event(db, int(str(form.get("event_id"))))
        driver_id = str(form.get("driver_id", ""))
        element = penalties.norm_element(str(form.get("element", "")))
        previous = penalties.previous_counts(db, event).get((driver_id, element))
        row = penalties.set_manual_usage(
            db, event, driver_id, element, int(str(form.get("count"))), user.id
        )
    except ValueError as exc:
        db.rollback()
        flash(request, f"Not saved: {exc}")
        return _admin_back(form.get("season"), "#usage")
    db.commit()
    changes = penalty_watch.Changes(event.id)
    if row.count > (previous or 0):
        changes.usage.append((row.id, previous))
    await _alert_manual(changes)
    flash(request, "Power unit usage saved.")
    return _admin_back(event.season, "#usage")


@router.post("/admin/penalties/grid")
async def penalties_grid(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    try:
        event = _event(db, int(str(form.get("event_id"))))
        row = penalties.add_manual_grid_penalty(
            db,
            event,
            str(form.get("driver_id", "")),
            _int_or_none(form.get("places")),
            bool(form.get("back_of_grid")),
            str(form.get("reason", "")),
            user.id,
        )
    except ValueError as exc:
        db.rollback()
        flash(request, f"Not saved: {exc}")
        return _admin_back(form.get("season"), "#grid")
    db.commit()
    await _alert_manual(penalty_watch.Changes(event.id, grid=[row.id]))
    flash(
        request, "Grid penalty saved." if row.places or row.back_of_grid else "Saved: no penalty."
    )
    return _admin_back(event.season, "#grid")


@router.post("/admin/penalties/points")
async def penalties_points(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    try:
        event_id = _int_or_none(form.get("event_id"))
        event = _event(db, event_id) if event_id else None
        driver_id = str(form.get("driver_id", ""))
        before = penalties.driver_total(db, driver_id)
        penalties.add_manual_points(
            db,
            driver_id,
            int(str(form.get("points"))),
            date.fromisoformat(str(form.get("awarded_on"))),
            str(form.get("reason", "")),
            event,
            user.id,
        )
        db.flush()
        crossings = penalty_watch.licence_crossings(db, {driver_id}, {driver_id: before})
    except ValueError as exc:
        db.rollback()
        flash(request, f"Not saved: {exc}")
        return _admin_back(form.get("season"), "#points")
    db.commit()
    alert_event = event or current_or_next_event(db)
    if crossings and alert_event is not None:
        await _alert_manual(penalty_watch.Changes(alert_event.id, licence=crossings))
    flash(request, "Licence points saved.")
    return _admin_back(form.get("season"), "#points")


_MANUAL_MODELS = {"usage": PuUsage, "grid": GridPenalty, "points": LicencePoints}


@router.post("/admin/penalties/{kind}/{row_id}/delete")
async def penalties_delete(
    request: Request,
    kind: str,
    row_id: int,
    db: DB = Depends(get_db),
    user: User = Depends(require_admin),
) -> Response:
    form = await checked_form(request)
    model = _MANUAL_MODELS.get(kind)
    if model is None:
        raise HTTPException(404)
    row = db.get(model, row_id)
    if row is not None and row.origin == "manual":
        db.delete(row)
        db.commit()
        flash(request, "Manual entry removed.")
    return _admin_back(form.get("season"), f"#{kind}")


# ---------------------------------------------------------------------------- news


def news_context(db: DB, user: User, settings: AppSettings, view: news.NewsView) -> dict[str, Any]:
    followed = {
        f"driver:{f.driver_id}"
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user.id))
    }
    return {
        "news": view,
        "news_labels": news.tag_labels(db, view.items),
        "followed_tags": followed,
        "news_tags": news.tags_of,
    }


@router.get("/news", response_class=HTMLResponse)
def news_page(
    request: Request,
    show: str = "all",
    reveal: bool = False,
    db: DB = Depends(get_db),
    user: User = Depends(require_user),
) -> HTMLResponse:
    settings = load_settings(db, user.id)
    followed = {
        f"driver:{f.driver_id}"
        for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user.id))
    }
    matchers = {
        "all": None,
        "followed": lambda tags: bool(followed & set(tags)),
        "track": lambda tags: "track" in tags,
    }
    if show not in matchers:
        show = "all"
    view = news.visible(db, user.id, settings, limit=100, match=matchers[show], reveal=reveal)
    feeds = list(db.scalars(select(NewsFeed).order_by(NewsFeed.name)))
    return page(
        request,
        "news.html",
        db,
        user,
        show=show,
        feeds=feeds,
        **news_context(db, user, settings, view),
    )


def _settings_back(anchor: str = "#news") -> Response:
    return back(f"/settings{anchor}")


@router.post("/admin/news/feeds")
async def news_add_feed(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    try:
        news.add_feed(db, str(form.get("url", "")), str(form.get("name", "")))
    except ValueError as exc:
        db.rollback()
        flash(request, f"Feed not added: {exc}")
        return _settings_back()
    db.commit()
    flash(request, "Feed added. It's checked within 30 minutes.")
    return _settings_back()


@router.post("/admin/news/feeds/{feed_id}/{action}")
async def news_feed_action(
    request: Request,
    feed_id: int,
    action: str,
    db: DB = Depends(get_db),
    user: User = Depends(require_admin),
) -> Response:
    await checked_form(request)
    feed = db.get(NewsFeed, feed_id)
    if feed is None or action not in ("toggle", "delete"):
        raise HTTPException(404)
    if action == "toggle":
        feed.enabled = not feed.enabled
    else:
        db.delete(feed)  # its headlines stay until they age out
    db.commit()
    return _settings_back()


@router.post("/admin/news/keywords")
async def news_keywords(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    words = []
    for line in str(form.get("keywords", "")).replace(",", "\n").splitlines():
        w = " ".join(line.split())[:60]
        if w and w.lower() not in {x.lower() for x in words}:
            words.append(w)
    set_kv(db, news.KEYWORDS_KEY, words[:50])
    db.commit()
    flash(request, "Track-change keywords saved (they apply to newly fetched headlines).")
    return _settings_back()
