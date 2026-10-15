"""Login, invites, account self-service and user administration."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env
from pitwall.db import get_db, utcnow
from pitwall.models import OWNER_ID, Invite, Outbox, User
from pitwall.users import (
    AccountError,
    accept_invite,
    admin_count,
    authenticate,
    create_invite,
    create_user,
    find_invite,
    generate_password,
    new_ics_token,
    set_password,
    verify_password,
)
from pitwall.web.auth import (
    checked_form,
    client_key,
    csrf_token,
    limiter,
    login_session,
    require_admin,
    require_user,
    safe_next,
    session_user,
)
from pitwall.web.common import back, flash, page, templates

log = logging.getLogger(__name__)
router = APIRouter()


def _login_page(
    request: Request, nxt: str, error: str | None = None, status: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf": csrf_token(request), "next": nxt, "error": error},
        status_code=status,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> Response:
    if session_user(request) is not None:
        return back(safe_next(next))
    return _login_page(request, safe_next(next))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: DB = Depends(get_db)) -> Response:
    form = await checked_form(request)
    key = client_key(request)
    nxt = safe_next(str(form.get("next", "/")))
    if limiter.blocked(key):
        log.warning("login rate limited", extra={"client": key})
        return _login_page(request, nxt, "Too many attempts. Try again in 15 minutes.", 429)
    username = str(form.get("username", ""))[:64]
    user = authenticate(db, username, str(form.get("password", "")))
    db.commit()
    if user is None:
        limiter.fail(key)
        log.warning("failed login", extra={"client": key, "username": username[:32]})
        # Same message whether the user exists, the password is wrong or the account is locked.
        return _login_page(
            request, nxt, "Wrong username or password (or the account is temporarily locked).", 401
        )
    limiter.reset(key)
    login_session(request, user)
    log.info("login", extra={"user": user.username})
    return back(nxt)


@router.post("/logout")
async def logout(request: Request) -> Response:
    await checked_form(request)
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------------------- invites


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_form(request: Request, token: str, db: DB = Depends(get_db)) -> HTMLResponse:
    inv = find_invite(db, token)
    reset_user = db.get(User, inv.user_id) if inv and inv.user_id else None
    return templates.TemplateResponse(
        request,
        "invite.html",
        {
            "csrf": csrf_token(request),
            "valid": inv is not None,
            "reset_user": reset_user,
            "token": token,
            "error": None,
        },
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
async def invite_accept(request: Request, token: str, db: DB = Depends(get_db)) -> Response:
    form = await checked_form(request)
    key = client_key(request)
    inv = find_invite(db, token)
    reset_user = db.get(User, inv.user_id) if inv and inv.user_id else None
    ctx = {
        "csrf": csrf_token(request),
        "valid": inv is not None,
        "reset_user": reset_user,
        "token": token,
    }
    if limiter.blocked(key):
        return templates.TemplateResponse(
            request,
            "invite.html",
            {**ctx, "error": "Too many attempts. Try again in 15 minutes."},
            status_code=429,
        )
    password = str(form.get("password", ""))
    if password != str(form.get("password2", "")):
        return templates.TemplateResponse(
            request, "invite.html", {**ctx, "error": "The passwords don't match."}, status_code=400
        )
    try:
        user = accept_invite(db, token, str(form.get("username", "")), password)
    except AccountError as exc:
        limiter.fail(key)
        db.rollback()
        return templates.TemplateResponse(
            request, "invite.html", {**ctx, "error": str(exc)}, status_code=400
        )
    db.commit()
    login_session(request, user)
    flash(
        request,
        f"Welcome, {user.username}! Set your timezone and add where your alerts should go below."
        if not reset_user
        else "Password updated.",
    )
    return back("/settings" if not reset_user else "/")


# ---------------------------------------------------------------------------- account


@router.get("/account", response_class=HTMLResponse)
def account(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> HTMLResponse:
    env = get_env()
    return page(
        request,
        "account.html",
        db,
        user,
        ics_url=f"{env.base_url.rstrip('/')}/calendar.ics?token={user.ics_token}"
        if user.ics_token
        else None,
        error=request.session.pop("account_error", None),
    )


@router.post("/account/password")
async def change_password(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    form = await checked_form(request)
    me = db.get(User, user.id)
    assert me is not None
    if not verify_password(str(form.get("current", "")), me.password_hash):
        request.session["account_error"] = "Your current password is wrong."
        return back("/account")
    new = str(form.get("password", ""))
    if new != str(form.get("password2", "")):
        request.session["account_error"] = "The new passwords don't match."
        return back("/account")
    try:
        set_password(db, me, new)
    except AccountError as exc:
        request.session["account_error"] = str(exc)
        return back("/account")
    db.commit()
    login_session(request, me)  # keep this session; all others are signed out
    flash(request, "Password changed. Other devices have been signed out.")
    return back("/account")


@router.post("/account/ics")
async def regenerate_ics(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    me = db.get(User, user.id)
    assert me is not None
    me.ics_token = new_ics_token()
    db.commit()
    flash(
        request,
        "New calendar link created. The old one has stopped working — update your "
        "calendar subscription.",
    )
    return back("/account#calendar")


@router.post("/account/signout-everywhere")
async def signout_everywhere(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    await checked_form(request)
    me = db.get(User, user.id)
    assert me is not None
    me.session_version += 1
    db.commit()
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------------------- admin: users


def _invite_url(token: str) -> str:
    return f"{get_env().base_url.rstrip('/')}/invite/{token}"


def _users_page(
    request: Request, db: DB, user: User, status: int = 200, **extra: object
) -> HTMLResponse:
    now = utcnow()
    invites = list(
        db.scalars(
            select(Invite)
            .where(Invite.used_at.is_(None), Invite.expires_at > now)
            .order_by(Invite.created_at.desc())
        )
    )
    resp = page(
        request,
        "users.html",
        db,
        user,
        users=list(db.scalars(select(User).order_by(User.id))),
        invites=invites,
        owner_id=OWNER_ID,
        now=now,
        new_link=request.session.pop("new_link", None),
        login_url=get_env().base_url.rstrip("/"),
        **extra,
    )
    resp.status_code = status
    return resp


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> HTMLResponse:
    return _users_page(request, db, user)


# Credentials are shown in the response itself (never stored, not even in the session cookie).
@router.post("/admin/users", response_class=HTMLResponse)
async def create_account(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> HTMLResponse:
    form = await checked_form(request)
    username = str(form.get("username", ""))[:64]
    password = str(form.get("password", "")) or generate_password(username.strip().lower())
    try:
        new = create_user(
            db,
            username,
            password,
            is_admin=bool(form.get("make_admin")),
            must_change_password=bool(form.get("must_change")),
        )
    except AccountError as exc:
        db.rollback()
        return _users_page(request, db, user, 400, create_error=str(exc), create_name=username)
    db.commit()
    log.info("account created", extra={"user": new.username, "by": user.username})
    creds = {"kind": "new", "username": new.username, "password": password}
    return _users_page(request, db, user, new_creds=creds)


@router.post("/admin/users/{user_id}/temp-password", response_class=HTMLResponse)
async def temp_password(
    request: Request, user_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    target = _other_user(db, user_id, user)
    if target.id == user.id:
        flash(request, "Change your own password under Account.")
        return back("/admin/users")
    password = generate_password(target.username)
    set_password(db, target, password, temporary=True)  # also unlocks and signs them out
    target.active = True
    db.commit()
    log.info("temporary password set", extra={"user": target.username, "by": user.username})
    creds = {"kind": "temp", "username": target.username, "password": password}
    return _users_page(request, db, user, new_creds=creds)


@router.post("/admin/invites")
async def new_invite(
    request: Request, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    form = await checked_form(request)
    token = create_invite(db, user, make_admin=bool(form.get("make_admin")))
    db.commit()
    request.session["new_link"] = {"kind": "invite", "url": _invite_url(token)}
    return back("/admin/users")


@router.post("/admin/invites/{invite_id}/revoke")
async def revoke_invite(
    request: Request, invite_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    inv = db.get(Invite, invite_id)
    if inv is not None:
        db.delete(inv)
        db.commit()
    flash(request, "Link revoked.")
    return back("/admin/users")


def _other_user(db: DB, user_id: int, me: User) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    return target


@router.post("/admin/users/{user_id}/reset")
async def reset_link(
    request: Request, user_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    target = _other_user(db, user_id, user)
    token = create_invite(db, user, reset_user=target)
    db.commit()
    request.session["new_link"] = {
        "kind": "reset",
        "user": target.username,
        "url": _invite_url(token),
    }
    return back("/admin/users")


@router.post("/admin/users/{user_id}/toggle-admin")
async def toggle_admin(
    request: Request, user_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    target = _other_user(db, user_id, user)
    if target.is_admin and (target.id == user.id or admin_count(db) <= 1):
        flash(request, "You can't remove your own admin rights or the last admin.")
        return back("/admin/users")
    target.is_admin = not target.is_admin
    target.session_version += 1
    db.commit()
    flash(
        request,
        f"{target.username} is {'now an admin' if target.is_admin else 'no longer an admin'}.",
    )
    return back("/admin/users")


@router.post("/admin/users/{user_id}/toggle-active")
async def toggle_active(
    request: Request, user_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    target = _other_user(db, user_id, user)
    if target.id == user.id:
        flash(request, "You can't disable your own account.")
        return back("/admin/users")
    if target.active and target.is_admin and admin_count(db) <= 1:
        flash(request, "You can't disable the last admin.")
        return back("/admin/users")
    target.active = not target.active
    target.session_version += 1  # signs them out
    db.commit()
    flash(request, f"{target.username} {'enabled' if target.active else 'disabled'}.")
    return back("/admin/users")


@router.post("/admin/users/{user_id}/delete")
async def delete_user(
    request: Request, user_id: int, db: DB = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    await checked_form(request)
    target = _other_user(db, user_id, user)
    if target.id in (user.id, OWNER_ID):
        flash(request, "You can't delete your own account or the original admin account.")
        return back("/admin/users")
    if target.is_admin and admin_count(db) <= 1:
        flash(request, "You can't delete the last admin.")
        return back("/admin/users")
    db.query(Outbox).filter(Outbox.user_id == target.id, Outbox.status == "pending").delete()
    name = target.username
    db.delete(target)  # settings, channels, followed drivers and invites go with it
    db.commit()
    flash(request, f"Deleted {name}.")
    return back("/admin/users")
