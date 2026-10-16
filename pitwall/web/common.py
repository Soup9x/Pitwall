"""Shared helpers for route modules."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DB

from pitwall import __version__
from pitwall.models import User
from pitwall.services import freshness
from pitwall.settings_store import get_kv, load_settings
from pitwall.templating import get_jinja_env
from pitwall.web.auth import csrf_token

templates = Jinja2Templates(env=get_jinja_env())


def page(request: Request, name: str, db: DB, user: User, **ctx: Any) -> HTMLResponse:
    base = {
        "settings": load_settings(db, user.id),
        "user": user,
        "csrf": csrf_token(request),
        "path": request.url.path,
        "version": __version__,
        "flash": request.session.pop("flash", None),
        "backfill": get_kv(db, "backfill_progress"),
        "fresh": freshness.all_of(db),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def back(to: str) -> RedirectResponse:
    return RedirectResponse(to, status_code=303)
