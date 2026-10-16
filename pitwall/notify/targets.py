"""Notification targets: builds Apprise URLs from simple form fields, validates them, and
combines targets saved in the UI with any APPRISE_URLS_* from .env."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import apprise
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env, split_urls
from pitwall.db import session_scope
from pitwall.logging_setup import register_secrets
from pitwall.models import OWNER_ID, NotificationTarget

EMAIL_KINDS = {
    "gmail": "Gmail",
    "m365": "Microsoft 365 / Outlook.com",
    "smtp": "Other SMTP server",
    "apprise": "Apprise URL",
}
WEBHOOK_KINDS = {
    "ntfy": "ntfy (phone push)",
    "discord": "Discord",
    "json": "Generic JSON webhook",
    "apprise": "Apprise URL",
}
KINDS = {"email": EMAIL_KINDS, "webhook": WEBHOOK_KINDS}
_DISCORD_RE = re.compile(r"/api/webhooks/(\d+)/([A-Za-z0-9_-]+)")
_OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


class TargetError(ValueError):
    """A user-facing problem with the submitted channel details."""


def _q(s: str) -> str:
    return quote(s, safe="")


def _field(form: Mapping[str, str], name: str, label: str, required: bool = True) -> str:
    value = (form.get(name) or "").strip()
    if required and not value:
        raise TargetError(f"{label} is required.")
    return value


def _email_address(form: Mapping[str, str], name: str, label: str, required: bool = True) -> str:
    value = _field(form, name, label, required)
    if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise TargetError(f"{label} doesn't look like an email address.")
    return value


@dataclass
class Built:
    url: str
    label: str
    note: str | None = None  # shown once after saving (e.g. the ntfy topic to subscribe to)


def build(channel: str, kind: str, form: Mapping[str, str]) -> Built:
    if kind not in KINDS.get(channel, {}):
        raise TargetError("Unknown channel type.")
    built = _build_email(kind, form) if channel == "email" else _build_webhook(kind, form)
    validate(built.url)
    return built


def _build_email(kind: str, form: Mapping[str, str]) -> Built:
    if kind == "apprise":
        return Built(_field(form, "apprise_url", "Apprise URL"), "Email (Apprise URL)")
    if kind == "smtp":
        return _build_smtp(form)

    addr = _email_address(form, "address", "Email address")
    password = _field(form, "password", "App password" if kind == "gmail" else "Password")
    to = _email_address(form, "to_addr", "Send to", required=False) or addr
    user, domain = addr.split("@", 1)
    if kind == "gmail":
        password = password.replace(" ", "")
        if domain.lower() in ("gmail.com", "googlemail.com"):
            url = f"mailto://{_q(user)}:{_q(password)}@gmail.com"
            url += f"?to={_q(to)}" if to.lower() != addr.lower() else ""
        else:  # Google Workspace address
            url = (
                f"mailtos://{_q(addr)}:{_q(password)}@{domain}"
                f"?smtp=smtp.gmail.com&mode=starttls&to={_q(to)}"
            )
        return Built(url, f"Gmail ({addr}) → {to}")
    smtp = "smtp-mail.outlook.com" if domain.lower() in _OUTLOOK_DOMAINS else "smtp.office365.com"
    url = f"mailtos://{_q(addr)}:{_q(password)}@{domain}?smtp={smtp}&mode=starttls&to={_q(to)}"
    return Built(url, f"Microsoft ({addr}) → {to}")


SMTP_SECURITY = {"starttls": "STARTTLS", "ssl": "SSL/TLS", "none": "None (local relay)"}


def _build_smtp(form: Mapping[str, str]) -> Built:
    raw = _field(form, "smtp_host", "SMTP server")
    # Accept "smtp.example.com", "smtp.example.com:587" or "smtp://smtp.example.com".
    host = re.sub(r"^[a-z]+://", "", raw.strip(), flags=re.I).strip("/")
    port = _field(form, "smtp_port", "SMTP port", required=False)
    if ":" in host and not host.startswith("["):
        host, _, host_port = host.rpartition(":")
        port = port or host_port
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise TargetError("The SMTP server should be a host name like smtp.example.com.")
    security = (form.get("smtp_security") or "").strip() or (
        "ssl" if port == "465" else "none" if port == "25" else "starttls"
    )
    if security not in SMTP_SECURITY:
        raise TargetError("Pick STARTTLS, SSL/TLS or None for security.")
    port = port or {"ssl": "465", "none": "25"}.get(security, "587")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise TargetError("SMTP port must be a number.")
    user = _field(form, "username", "Username", required=False)
    password = _field(form, "password", "Password", required=False)
    if bool(user) != bool(password):
        raise TargetError(
            "Enter both a username and a password, or leave both empty for a relay without login."
        )
    sender = _email_address(form, "from_addr", "From address")
    to = _email_address(form, "to_addr", "Send to", required=False) or sender
    login = f"{_q(user)}:{_q(password)}@" if user else ""
    scheme = "mailto" if security == "none" else "mailtos"
    mode = "" if security == "none" else f"&mode={security}"
    url = (
        f"{scheme}://{login}{sender.split('@')[1]}:{port}"
        f"?smtp={host}{mode}&from={_q(sender)}&to={_q(to)}"
    )
    return Built(url, f"SMTP via {host} → {to}")


def _build_webhook(kind: str, form: Mapping[str, str]) -> Built:
    if kind == "apprise":
        return Built(_field(form, "apprise_url", "Apprise URL"), "Webhook (Apprise URL)")
    if kind == "ntfy":
        server = _field(form, "ntfy_server", "ntfy server", required=False) or "ntfy.sh"
        server = re.sub(r"^https?://", "", server).rstrip("/")
        topic = (
            _field(form, "ntfy_topic", "Topic", required=False) or f"pitwall-{secrets.token_hex(5)}"
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise TargetError("ntfy topics may only use letters, numbers, - and _.")
        url = f"ntfy://{topic}" if server == "ntfy.sh" else f"ntfys://{server}/{topic}"
        return Built(
            url,
            f"ntfy ({server})",
            note=f"Subscribe to the topic “{topic}” in the ntfy app (server: {server}).",
        )
    if kind == "discord":
        m = _DISCORD_RE.search(_field(form, "discord_url", "Discord webhook URL"))
        if not m:
            raise TargetError(
                "That doesn't look like a Discord webhook URL "
                "(https://discord.com/api/webhooks/<id>/<token>)."
            )
        return Built(f"discord://{m.group(1)}/{m.group(2)}", "Discord")
    url = _field(form, "json_url", "Webhook URL")
    if url.startswith("https://"):
        return Built(
            "jsons://" + url[len("https://") :], f"JSON webhook ({urlsplit(url).hostname})"
        )
    if url.startswith("http://"):
        return Built("json://" + url[len("http://") :], f"JSON webhook ({urlsplit(url).hostname})")
    raise TargetError("The webhook URL must start with http:// or https://.")


def validate(url: str) -> None:
    from pitwall.notify.dispatch import _rewrite  # registers the custom JSON plugin

    rewritten, _ = _rewrite(url)
    if not apprise.Apprise().add(rewritten):
        raise TargetError(
            "Apprise doesn't recognise that URL. See https://github.com/caronc/apprise/wiki"
        )


def url_secrets(url: str) -> list[str]:
    """The URL plus its password/token parts, for log redaction."""
    out = [url]
    try:
        parts = urlsplit(url)
    except ValueError:
        return out
    if parts.password:
        out += [parts.password, unquote(parts.password)]
    if parts.scheme == "discord" and parts.path:
        out.append(parts.path.strip("/"))
    return [s for s in out if len(s) >= 4]


def env_urls(channel: str) -> list[str]:
    env = get_env()
    return split_urls(env.apprise_urls_email if channel == "email" else env.apprise_urls_webhook)


def db_targets(
    db: DB, channel: str | None = None, user_id: int | None = OWNER_ID
) -> list[NotificationTarget]:
    """A user's targets (user_id=None: everyone's)."""
    q = select(NotificationTarget).order_by(NotificationTarget.channel, NotificationTarget.id)
    if user_id is not None:
        q = q.where(NotificationTarget.user_id == user_id)
    if channel:
        q = q.where(NotificationTarget.channel == channel)
    return list(db.scalars(q))


def server_email_url(db: DB, user_id: int) -> str | None:
    """The user's alert address(es) sent through the server's mail account, if both are set."""
    from pitwall.notify import mailserver
    from pitwall.settings_store import get_user_kv

    server = mailserver.load(db)
    if server is None:
        return None
    try:
        to = mailserver.parse_addresses(get_user_kv(db, user_id, "notify_email", "") or "")
    except TargetError:
        return None
    return server.url(to) if to else None


def channel_urls(channel: str, db: DB | None = None, user_id: int | None = None) -> list[str]:
    """Every enabled destination of a user on a channel: their address via the server's mail
    account, channels they added themselves, and (for the owner) URLs from .env."""
    uid = user_id or OWNER_ID
    if db is None:
        with session_scope() as s:
            return channel_urls(channel, s, uid)
    urls = [t.url for t in db_targets(db, channel, uid) if t.enabled]
    if channel == "email" and (server_url := server_email_url(db, uid)):
        urls.insert(0, server_url)
    if uid == OWNER_ID:
        urls += [u for u in env_urls(channel) if u not in urls]
    return urls


def register_all_secrets(db: DB) -> None:
    from pitwall.notify import mailserver

    for t in db_targets(db, user_id=None):
        register_secrets(url_secrets(t.url))
    mailserver.register(mailserver.load(db))
