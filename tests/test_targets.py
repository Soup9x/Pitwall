"""Notification channels configured in the Settings UI."""

from __future__ import annotations

import re

import apprise
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import NotificationLog, NotificationTarget
from pitwall.notify import targets
from pitwall.notify.targets import TargetError, build
from pitwall.web import auth

PW = "p@ss:w/rd#1&?"


def parsed(url):
    a = apprise.Apprise()
    assert a.add(url), url
    return a[0]


def test_gmail():
    b = build("email", "gmail", {"address": "me.name@gmail.com", "password": "abcd efgh ijkl mnop"})
    p = parsed(b.url)
    assert (p.smtp_host, p.user, p.password) == (
        "smtp.gmail.com",
        "me.name@gmail.com",
        "abcdefghijklmnop",
    )
    assert b.label == "Gmail (me.name@gmail.com) → me.name@gmail.com"
    b = build(
        "email",
        "gmail",
        {"address": "boss@company.com", "password": "x" * 16, "to_addr": "team@company.com"},
    )
    p = parsed(b.url)
    assert p.smtp_host == "smtp.gmail.com" and p.targets == [(False, "team@company.com")]


def test_microsoft_and_smtp_with_special_characters():
    p = parsed(build("email", "m365", {"address": "me@contoso.com", "password": PW}).url)
    assert (p.smtp_host, p.user, p.password, p.secure_mode) == (
        "smtp.office365.com",
        "me@contoso.com",
        PW,
        "starttls",
    )
    p = parsed(build("email", "m365", {"address": "me@outlook.com", "password": PW}).url)
    assert p.smtp_host == "smtp-mail.outlook.com"
    p = parsed(
        build(
            "email",
            "smtp",
            {
                "smtp_host": "mail.example.org",
                "smtp_port": "465",
                "username": "u",
                "password": PW,
                "from_addr": "alerts@example.org",
                "to_addr": "me@example.com",
            },
        ).url
    )
    assert (p.smtp_host, p.port, p.secure_mode, p.password) == ("mail.example.org", 465, "ssl", PW)
    assert p.targets == [(False, "me@example.com")]


def test_webhooks():
    b = build("webhook", "ntfy", {"ntfy_topic": "my_topic"})
    assert b.url == "ntfy://my_topic" and "my_topic" in b.note
    assert (
        build(
            "webhook", "ntfy", {"ntfy_server": "https://ntfy.example.com/", "ntfy_topic": "t1"}
        ).url
        == "ntfys://ntfy.example.com/t1"
    )
    assert re.fullmatch(r"ntfy://pitwall-[0-9a-f]{10}", build("webhook", "ntfy", {}).url)
    assert (
        build(
            "webhook",
            "discord",
            {"discord_url": "https://discord.com/api/webhooks/123456/abc-DEF_1"},
        ).url
        == "discord://123456/abc-DEF_1"
    )
    assert (
        build("webhook", "json", {"json_url": "https://ha.local/api/hook"}).url
        == "jsons://ha.local/api/hook"
    )
    assert (
        build("webhook", "json", {"json_url": "http://10.0.0.5:5678/x"}).url
        == "json://10.0.0.5:5678/x"
    )


@pytest.mark.parametrize(
    "channel,kind,form,msg",
    [
        ("email", "gmail", {"password": "x"}, "Email address is required"),
        ("email", "gmail", {"address": "nope", "password": "x"}, "doesn't look like an email"),
        (
            "email",
            "smtp",
            {
                "smtp_host": "h",
                "smtp_port": "abc",
                "username": "u",
                "password": "p",
                "from_addr": "a@b.co",
            },
            "port must be a number",
        ),
        ("webhook", "discord", {"discord_url": "https://example.com"}, "Discord webhook URL"),
        ("webhook", "json", {"json_url": "ftp://x"}, "must start with http"),
        ("webhook", "ntfy", {"ntfy_topic": "bad topic!"}, "ntfy topics"),
        ("webhook", "apprise", {"apprise_url": "nonsense://"}, "doesn't recognise"),
        ("webhook", "gmail", {}, "Unknown channel type"),
    ],
)
def test_validation_errors(channel, kind, form, msg):
    with pytest.raises(TargetError, match=msg):
        build(channel, kind, form)


def test_channel_urls_combine_ui_and_env(db):
    db.add(NotificationTarget(channel="webhook", kind="ntfy", label="a", url="ntfy://abcd"))
    db.add(
        NotificationTarget(
            channel="webhook", kind="ntfy", label="b", url="ntfy://off", enabled=False
        )
    )
    db.flush()
    assert targets.channel_urls("webhook", db) == [
        "ntfy://abcd",
        "jsons://hooks.example.com/pitwall",
    ]


def test_mask_hides_ntfy_topic():
    from pitwall.notify.dispatch import mask_url

    assert mask_url("ntfy://pitwall-0123456789") == "ntfy://pitw…/…"


# ---- web


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        c.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        c.csrf = re.search(
            r'name="csrf_token" value="([^"]+)"',  # type: ignore[attr-defined]
            c.get("/settings").text,
        ).group(1)
        yield c


def test_add_test_toggle_remove_via_ui(client, db):
    r = client.post(
        "/settings/targets",
        data={
            "csrf_token": client.csrf,
            "channel": "email",
            "kind": "m365",
            "address": "me@contoso.com",
            "password": PW,
            "label": "",
        },
    )
    assert "Added Microsoft (me@contoso.com)" in r.text
    t = db.scalars(select(NotificationTarget)).one()
    page = client.get("/settings").text
    assert PW not in page and "p%40ss" not in page and "mailtos://contoso.com/…" in page

    # Per-target test goes only to that target.
    client.post(
        "/settings/targets",
        data={
            "csrf_token": client.csrf,
            "channel": "webhook",
            "kind": "json",
            "json_url": "https://hook.example.net/x",
        },
    )
    hook = db.scalars(select(NotificationTarget).where(NotificationTarget.kind == "json")).one()
    with respx.mock() as mock:
        route = mock.post("https://hook.example.net/x").mock(return_value=httpx.Response(200))
        r = client.post(f"/settings/targets/{hook.id}/test", data={"csrf_token": client.csrf})
    assert route.call_count == 1 and "sent ✅" in r.text
    db.expire_all()
    log = db.scalars(select(NotificationLog)).one()
    assert log.status == "sent" and "JSON webhook" in log.title

    client.post(f"/settings/targets/{t.id}/toggle", data={"csrf_token": client.csrf})
    db.expire_all()
    assert db.get(NotificationTarget, t.id).enabled is False
    assert targets.channel_urls("email", db) == ["mailtos://user:pass@smtp.example.com"]

    tid = t.id
    client.post(f"/settings/targets/{tid}/delete", data={"csrf_token": client.csrf})
    db.expunge_all()
    assert (
        db.scalars(select(NotificationTarget).where(NotificationTarget.id == tid)).first() is None
    )


def test_bad_target_shows_error_and_saves_nothing(client, db):
    r = client.post(
        "/settings/targets",
        data={
            "csrf_token": client.csrf,
            "channel": "webhook",
            "kind": "discord",
            "discord_url": "https://example.com/nope",
        },
    )
    assert "Couldn&#39;t add webhook" in r.text
    assert db.scalars(select(NotificationTarget)).first() is None


def test_target_routes_need_csrf(client, db):
    r = client.post("/settings/targets", data={"channel": "webhook", "kind": "ntfy"})
    assert r.status_code == 403


def test_secret_parts_are_redacted_from_logs(capsys):
    import json
    import logging

    from pitwall.logging_setup import configure_logging, register_secrets

    configure_logging("INFO", [])
    url = build("email", "m365", {"address": "me@contoso.com", "password": PW}).url
    register_secrets(targets.url_secrets(url))
    logging.getLogger("t").warning("oops %s / %s", PW, url)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert PW not in line["msg"] and "p%40ss" not in line["msg"]
    logging.getLogger().handlers.clear()
