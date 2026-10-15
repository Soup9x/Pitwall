"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from pitwall import scheduler
from pitwall.config import get_env
from pitwall.db import init_engine, session_scope
from pitwall.logging_setup import configure_logging
from pitwall.models import OWNER_ID, User
from pitwall.notify.targets import register_all_secrets
from pitwall.services.circuits import merge_all
from pitwall.users import bootstrap
from pitwall.web import accounts, routes
from pitwall.web.auth import NotAuthenticated
from pitwall.web.security import SecurityHeadersMiddleware

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(*, manage_db: bool = True, start_scheduler: bool | None = None) -> FastAPI:
    env = get_env()
    run_scheduler = env.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_db:
            configure_logging(env.log_level, env.secret_values())
            env.data_dir.mkdir(parents=True, exist_ok=True)
            engine = init_engine(env.db_url)
            from pitwall.migrate import upgrade

            upgrade(engine)
        with session_scope() as db:
            bootstrap(db)
            owner = db.get(User, OWNER_ID)
            if owner is not None and owner.password_hash == "!":
                log.warning(
                    "the admin account has no password: set PITWALL_ADMIN_PASSWORD or "
                    "run `python -m pitwall set-password admin`"
                )
            merge_all(db)
            register_all_secrets(db)
        if run_scheduler:
            scheduler.start()
        log.info("pitwall started", extra={"base_url": env.base_url, "tz": env.tz})
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(
        title="Pitwall", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=env.resolved_secret_key(),
        session_cookie="pitwall_session",
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=env.secure_cookies,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=env.secure_cookies)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(exc.next_path)}", status_code=303)

    app.include_router(routes.public)
    app.include_router(accounts.router)
    app.include_router(routes.private)
    app.include_router(routes.admin)
    return app
