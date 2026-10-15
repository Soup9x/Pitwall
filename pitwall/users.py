"""User accounts: password hashing, bootstrap of the owner account, invites and lockout."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env
from pitwall.db import utcnow
from pitwall.models import OWNER_ID, Invite, User

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 10
LOCKOUT_FAILURES = 5
LOCKOUT_MINUTES = 15
INVITE_DAYS = 7
RESET_HOURS = 48

# scrypt parameters: ~16 MB of memory per hash, about 50 ms on a small server.
_N, _R, _P = 2**14, 8, 1


class AccountError(ValueError):
    """A user-facing problem (bad username, weak password, expired invite...)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Used to spend the same time on unknown usernames as on real ones.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise AccountError("Usernames are 2–32 characters: letters, numbers, '.', '-' or '_'.")
    return username


def check_password(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords must be at least {MIN_PASSWORD} characters.")
    if username and username.lower() in password.lower():
        raise AccountError("The password mustn't contain the username.")
    if len(set(password)) < 4:
        raise AccountError("That password is too simple.")


def new_ics_token() -> str:
    return secrets.token_hex(24)


def bootstrap(db: DB) -> None:
    """Give the owner account (created by migration 0005) its password and calendar token.

    The password comes from PITWALL_ADMIN_PASSWORD the first time only; after that it's managed
    in the UI (or reset with `python -m pitwall set-password admin`). The calendar token reuses
    PITWALL_ICS_TOKEN so existing Google Calendar subscriptions keep working."""
    env = get_env()
    owner = db.get(User, OWNER_ID)
    if owner is None:
        return
    if owner.password_hash == "!" and env.admin_password:
        owner.password_hash = hash_password(env.admin_password)
    if not owner.ics_token:
        owner.ics_token = env.ics_token or new_ics_token()
    db.flush()


def authenticate(db: DB, username: str, password: str, now: datetime | None = None) -> User | None:
    """Returns the user, or None. Locks an account for 15 minutes after 5 failures."""
    now = now or utcnow()
    user = db.scalars(select(User).where(User.username == username.strip().lower())).first()
    if user is None or not user.active:
        verify_password(password, _DUMMY_HASH)
        return None
    if user.locked_until and user.locked_until > now:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= LOCKOUT_FAILURES:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
        db.flush()
        return None
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.flush()
    return user


def set_password(db: DB, user: User, password: str) -> None:
    check_password(password, user.username)
    user.password_hash = hash_password(password)
    user.session_version += 1  # signs out every other session
    user.failed_logins = 0
    user.locked_until = None
    db.flush()


def create_user(db: DB, username: str, password: str, *, is_admin: bool = False) -> User:
    username = check_username(username)
    if db.scalars(select(User).where(User.username == username)).first():
        raise AccountError("That username is taken.")
    check_password(password, username)
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
        ics_token=new_ics_token(),
    )
    db.add(user)
    db.flush()
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(
    db: DB,
    created_by: User,
    *,
    make_admin: bool = False,
    reset_user: User | None = None,
    now: datetime | None = None,
) -> str:
    """Returns the one-time token (only its hash is stored)."""
    now = now or utcnow()
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(hours=RESET_HOURS) if reset_user else timedelta(days=INVITE_DAYS)
    db.add(
        Invite(
            token_hash=_token_hash(token),
            user_id=reset_user.id if reset_user else None,
            make_admin=make_admin,
            created_by=created_by.id,
            created_at=now,
            expires_at=now + lifetime,
        )
    )
    db.flush()
    return token


def find_invite(db: DB, token: str, now: datetime | None = None) -> Invite | None:
    now = now or utcnow()
    inv = db.scalars(select(Invite).where(Invite.token_hash == _token_hash(token))).first()
    if inv is None or inv.used_at is not None or inv.expires_at <= now:
        return None
    return inv


def accept_invite(
    db: DB, token: str, username: str, password: str, now: datetime | None = None
) -> User:
    now = now or utcnow()
    inv = find_invite(db, token, now)
    if inv is None:
        raise AccountError("This link has expired or was already used. Ask for a new one.")
    if inv.user_id is not None:  # password reset
        user = db.get(User, inv.user_id)
        if user is None or not user.active:
            raise AccountError("This account no longer exists.")
        set_password(db, user, password)
    else:
        user = create_user(db, username, password, is_admin=inv.make_admin)
    inv.used_at = now
    db.flush()
    return user


def admin_count(db: DB) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin, User.active)) or 0


def active_users(db: DB) -> list[User]:
    return list(db.scalars(select(User).where(User.active).order_by(User.id)))
