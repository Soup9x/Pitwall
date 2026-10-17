"""The server's outgoing email (SMTP) account, set up once by an admin.

Every user's email alerts are sent through it; users only choose the address(es) their alerts
go to (the per-user `notify_email` setting). The password is stored in Pitwall's database like
other channel secrets, is never shown again after saving, and is redacted from the logs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy.orm import Session as DB

from pitwall.logging_setup import register_secrets
from pitwall.models import OWNER_ID
from pitwall.notify.targets import (
    SMTP_SECURITY,
    TargetError,
    _q,
    db_targets,
    env_urls,
    validate,
)
from pitwall.settings_store import get_kv, get_user_kv, load_settings, save_settings, set_kv

KEY = "mail_server"
PROVIDERS = {
    "gmail": "Gmail / Google Workspace",
    "m365": "Microsoft 365 / Outlook.com",
    "smtp": "Other SMTP server",
}
PRESETS = {
    "gmail": ("smtp.gmail.com", 587, "starttls"),
    "m365": ("smtp.office365.com", 587, "starttls"),
}
_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
MAX_RECIPIENTS = 3


@dataclass
class MailServer:
    provider: str
    host: str
    port: int
    security: str  # starttls / ssl / none
    username: str
    password: str
    from_addr: str
    from_name: str = "Pitwall"

    @property
    def summary(self) -> str:
        login = f" as {self.username}" if self.username else ""
        return f"{self.host}:{self.port}{login}, from {self.from_name} <{self.from_addr}>"

    def url(self, to: list[str]) -> str:
        """An Apprise mailto URL that sends to `to` (one user's addresses)."""
        login = f"{_q(self.username)}:{_q(self.password)}@" if self.username else ""
        scheme = "mailto" if self.security == "none" else "mailtos"
        mode = "" if self.security == "none" else f"&mode={self.security}"
        domain = self.from_addr.split("@", 1)[1]
        return (
            f"{scheme}://{login}{domain}:{self.port}?smtp={self.host}{mode}"
            f"&from={_q(self.from_addr)}&name={_q(self.from_name)}&to={_q(','.join(to))}"
        )


def load(db: DB) -> MailServer | None:
    raw = get_kv(db, KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return MailServer(**raw)
    except TypeError:
        return None


def register(server: MailServer | None) -> None:
    if server is not None and server.password:
        register_secrets([server.password])


def parse_addresses(raw: str) -> list[str]:
    """'a@x.com, b@y.com' -> ['a@x.com', 'b@y.com']. Raises TargetError for anything odd."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]
    for p in parts:
        if not _EMAIL.fullmatch(p):
            raise TargetError(f"“{p[:60]}” doesn't look like an email address.")
    if len(parts) > MAX_RECIPIENTS:
        raise TargetError(f"Use at most {MAX_RECIPIENTS} addresses.")
    seen: list[str] = []
    for p in parts:
        if p.lower() not in (s.lower() for s in seen):
            seen.append(p)
    return seen


def save(db: DB, form: Mapping[str, str]) -> MailServer:
    """Validate the admin form and store it. A blank password keeps the saved one."""
    provider = (form.get("provider") or "").strip()
    if provider not in PROVIDERS:
        raise TargetError("Pick an email provider.")
    current = load(db)
    if provider in PRESETS:
        host, port, security = PRESETS[provider]
    else:
        raw = (form.get("smtp_host") or "").strip()
        host = re.sub(r"^[a-z]+://", "", raw, flags=re.I).strip("/")
        port_s = (form.get("smtp_port") or "").strip()
        if ":" in host:
            host, _, host_port = host.rpartition(":")
            port_s = port_s or host_port
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise TargetError("The SMTP server should be a host name like smtp.example.com.")
        security = (form.get("smtp_security") or "").strip() or (
            "ssl" if port_s == "465" else "none" if port_s == "25" else "starttls"
        )
        if security not in SMTP_SECURITY:
            raise TargetError("Pick STARTTLS, SSL/TLS or None for security.")
        port_s = port_s or {"ssl": "465", "none": "25"}.get(security, "587")
        if not port_s.isdigit() or not 0 < int(port_s) < 65536:
            raise TargetError("The SMTP port must be a number.")
        port = int(port_s)
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    if provider == "gmail":
        password = password.replace(" ", "")  # Google shows app passwords in groups of four
    if not password and current is not None and username == current.username:
        password = current.password  # blank = keep the saved password
    if provider in PRESETS and not (username and password):
        raise TargetError(
            "Enter the account's email address and an app password."
            if provider == "gmail"
            else "Enter the mailbox's email address and password."
        )
    if bool(username) != bool(password):
        raise TargetError(
            "Enter both a username and a password, or leave both empty for a relay without login."
        )
    from_addr = (form.get("from_addr") or "").strip() or (username if "@" in username else "")
    if not _EMAIL.fullmatch(from_addr):
        raise TargetError("Enter the address emails should come from, e.g. pitwall@yourdomain.com.")
    from_name = (form.get("from_name") or "").strip()[:60] or "Pitwall"
    server = MailServer(provider, host, port, security, username, password, from_addr, from_name)
    validate(server.url([from_addr]))
    set_kv(db, KEY, asdict(server))
    register(server)
    return server


def clear(db: DB) -> None:
    set_kv(db, KEY, None)


def from_target_url(url: str) -> tuple[MailServer, list[str]] | None:
    """Turn an email channel saved in the old per-user form (an Apprise mailto URL) into server
    settings plus the address it sent to. None if it isn't a plain SMTP login URL."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme not in ("mailto", "mailtos") or not p.username or not p.password:
        return None
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    domain = (p.hostname or "").lower()
    username = unquote(p.username)
    if "@" not in username:
        username = f"{username}@{domain}"
    host = q.get("smtp") or ("smtp.gmail.com" if domain in ("gmail.com", "googlemail.com") else "")
    if not host:
        return None
    # Apprise knows gmail.com by name ("mailto://user:pw@gmail.com"): always STARTTLS on 587.
    implicit_gmail = "smtp" not in q and host == "smtp.gmail.com"
    security = q.get("mode") or ("starttls" if p.scheme == "mailtos" or implicit_gmail else "none")
    if security not in SMTP_SECURITY:
        security = "starttls"
    port = p.port or {"ssl": 465, "none": 25}.get(security, 587)
    provider = (
        "gmail"
        if host == "smtp.gmail.com"
        else "m365"
        if "office365" in host or ("outlook" in host)
        else "smtp"
    )
    from_addr = q.get("from") or username
    to = [a for a in re.split(r"[,\s]+", q.get("to") or from_addr) if a]
    server = MailServer(
        provider, host, port, security, username, unquote(p.password), from_addr, "Pitwall"
    )
    return server, to


def describe(server: MailServer | None) -> dict[str, Any] | None:
    """What the admin form shows: everything except the password."""
    if server is None:
        return None
    out = asdict(server)
    out.pop("password")
    out["has_password"] = bool(server.password)
    out["spoofing_risk"] = spoofing_risk(server)
    return out


PERSONAL_GOOGLE = ("gmail.com", "googlemail.com")


def spoofing_risk(server: MailServer) -> bool:
    """A personal Gmail account sending as an address on another domain. Google sends it, but
    the domain's anti-spoofing checks (SPF/DKIM/DMARC) can't pass, so other providers often
    reject it or file it as spam, while mail to the sending Gmail account itself arrives."""
    login_domain = server.username.rpartition("@")[2].lower()
    from_domain = server.from_addr.rpartition("@")[2].lower()
    return (
        server.host == "smtp.gmail.com"
        and login_domain in PERSONAL_GOOGLE
        and from_domain not in PERSONAL_GOOGLE
    )


def set_user_addresses(db: DB, user_id: int, addresses: list[str]) -> bool:
    """Save where a user's email alerts go. The first time a user without any webhooks (and
    without routing choices of their own) adds an address, every alert type they'd get is
    routed to email too, so email-only users don't miss reminders and results. Returns
    whether the routing changed."""
    first_time = not load_settings(db, user_id).notify_email
    save_settings(db, {"notify_email": ", ".join(addresses)}, user_id)
    if not addresses or not first_time:
        return False
    if db_targets(db, "webhook", user_id) or get_user_kv(db, user_id, "routing"):
        return False
    if user_id == OWNER_ID and env_urls("webhook"):
        return False
    routing = {
        alert: sorted({*chans, "email"}) if chans else chans
        for alert, chans in load_settings(db, user_id).routing.items()
    }
    save_settings(db, {"routing": routing}, user_id)
    return True
