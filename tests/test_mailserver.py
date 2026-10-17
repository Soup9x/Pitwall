"""Server-wide outgoing email: an admin sets up one SMTP account (e.g. Gmail sending as an
address on their own domain); every user only enters the address their alerts go to."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from pitwall.models import NotificationTarget, User
from pitwall.notify import mailserver
from pitwall.notify import targets as targets_svc
from pitwall.notify.targets import TargetError
from pitwall.settings_store import load_settings
from pitwall.users import create_invite, create_user
from pitwall.web import auth, routes

PASSWORD = "correct horse battery"
GMAIL = {
    "provider": "gmail",
    "username": "me@gmail.com",
    "password": "abcd efgh ijkl mnop",
    "from_addr": "pitwall@example.org",
    "from_name": "Pitwall F1",
}


def _q(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_gmail_preset_sends_as_your_own_domain(db):
    server = mailserver.save(db, GMAIL)
    assert (server.host, server.port, server.security) == ("smtp.gmail.com", 587, "starttls")
    assert server.password == "abcdefghijklmnop"  # Google shows app passwords with spaces
    url = server.url(["fan@example.com"])
    q = _q(url)
    assert q["smtp"] == "smtp.gmail.com" and q["mode"] == "starttls"
    assert q["from"] == "pitwall@example.org" and q["name"] == "Pitwall F1"
    assert q["to"] == "fan@example.com"
    assert unquote(urlsplit(url).username) == "me@gmail.com"
    # Saving again without a password keeps the stored one.
    again = mailserver.save(db, {**GMAIL, "password": "", "from_name": "Pitwall"})
    assert again.password == "abcdefghijklmnop" and again.from_name == "Pitwall"
    assert mailserver.describe(again).get("password") is None


@pytest.mark.parametrize(
    ("form", "msg"),
    [
        ({**GMAIL, "password": ""}, "app password"),
        ({**GMAIL, "from_addr": "nope"}, "come from"),
        ({**GMAIL, "provider": "pigeon"}, "provider"),
        ({"provider": "smtp", "smtp_host": "bad host!", "from_addr": "a@b.co"}, "host name"),
        (
            {"provider": "smtp", "smtp_host": "mail.x.org", "username": "u", "from_addr": "a@b.co"},
            "both",
        ),
    ],
)
def test_bad_settings_are_explained(db, form, msg):
    with pytest.raises(TargetError, match=msg):
        mailserver.save(db, form)


def test_other_smtp_server(db):
    server = mailserver.save(
        db, {"provider": "smtp", "smtp_host": "mail.example.org:465", "from_addr": "f1@example.org"}
    )
    assert (server.host, server.port, server.security, server.username) == (
        "mail.example.org",
        465,
        "ssl",
        "",
    )


def test_addresses():
    assert mailserver.parse_addresses(" a@x.com, B@y.org;a@X.com ") == ["a@x.com", "B@y.org"]
    assert mailserver.parse_addresses("") == []
    with pytest.raises(TargetError, match="doesn't look like"):
        mailserver.parse_addresses("me at home")
    with pytest.raises(TargetError, match="at most"):
        mailserver.parse_addresses("a@x.com b@x.com c@x.com d@x.com")


def test_each_user_gets_their_own_address_via_the_server(db):
    friend = create_user(db, "friend", "a long password 1")
    assert targets_svc.channel_urls("email", db, friend.id) == []
    mailserver.save(db, GMAIL)
    assert targets_svc.channel_urls("email", db, friend.id) == []  # no address yet
    mailserver.set_user_addresses(db, friend.id, ["friend@example.com"])
    urls = targets_svc.channel_urls("email", db, friend.id)
    assert len(urls) == 1 and _q(urls[0])["to"] == "friend@example.com"
    owner_urls = targets_svc.channel_urls("email", db, 1)
    assert all("friend@example.com" not in u for u in owner_urls)  # never someone else's


def test_email_only_users_get_every_alert_by_email(db):
    friend = create_user(db, "friend", "a long password 1")
    assert mailserver.set_user_addresses(db, friend.id, ["friend@example.com"]) is True
    routing = load_settings(db, friend.id).routing
    assert "email" in routing["reminder"] and "email" in routing["results"]
    assert routing["pu_element"] == []  # off stays off
    # Only the first time, and never over choices the user made.
    assert mailserver.set_user_addresses(db, friend.id, ["new@example.com"]) is False
    other = create_user(db, "other", "a long password 2")
    db.add(
        NotificationTarget(
            user_id=other.id, channel="webhook", kind="ntfy", label="x", url="ntfy://t"
        )
    )
    db.flush()
    assert mailserver.set_user_addresses(db, other.id, ["o@example.com"]) is False


@pytest.mark.parametrize(
    ("kind", "form", "expect"),
    [
        (
            "gmail",
            {"address": "me@gmail.com", "password": "abcd efgh ijkl mnop", "to_addr": "fan@x.com"},
            ("smtp.gmail.com", 587, "starttls", "me@gmail.com", "me@gmail.com", ["fan@x.com"]),
        ),
        (
            "gmail",
            {"address": "me@example.org", "password": "abcdefghijklmnop"},
            (
                "smtp.gmail.com",
                587,
                "starttls",
                "me@example.org",
                "me@example.org",
                ["me@example.org"],
            ),
        ),
        (
            "smtp",
            {
                "smtp_host": "smtp.gmail.com",
                "username": "me@gmail.com",
                "password": "pw x",
                "from_addr": "pitwall@example.org",
                "to_addr": "fan@x.com",
            },
            (
                "smtp.gmail.com",
                587,
                "starttls",
                "me@gmail.com",
                "pitwall@example.org",
                ["fan@x.com"],
            ),
        ),
    ],
)
def test_existing_channels_convert_to_server_settings(kind, form, expect):
    built = targets_svc.build("email", kind, form)
    server, to = mailserver.from_target_url(built.url)
    assert (
        server.host,
        server.port,
        server.security,
        server.username,
        server.from_addr,
        to,
    ) == expect
    assert server.password.replace(" ", "") == form["password"].replace(" ", "")


# ---------------------------------------------------------------------------- web


def _login(c, username, password):
    c.cookies.clear()
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
    c.post("/login", data={"username": username, "password": password, "csrf_token": token})
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/settings").text)[1]


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


@pytest.fixture
def sends(monkeypatch):
    calls = []

    def fake(channel, msg, urls=None):
        calls.append((channel, urls))
        return True, "delivered"

    monkeypatch.setattr(routes, "dispatch_notify", fake)
    return calls


def test_only_admins_configure_the_server(client, db):
    create_user(db, "friend", "a long password 1")
    db.commit()
    csrf = _login(client, "friend", "a long password 1")
    page = client.get("/settings").text
    assert "Outgoing email" not in page and 'name="smtp_host"' not in page
    assert "Ask an admin" in page
    assert client.post("/admin/mail", data={**GMAIL, "csrf_token": csrf}).status_code == 403
    r = client.post(
        "/settings/targets",
        data={"csrf_token": csrf, "channel": "email", "kind": "smtp", "smtp_host": "x.org"},
    )
    assert r.status_code == 403
    assert mailserver.load(db) is None
    # Webhooks are personal destinations: still allowed.
    r = client.post(
        "/settings/targets",
        data={"csrf_token": csrf, "channel": "webhook", "kind": "ntfy", "ntfy_topic": "mine"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_admin_sets_up_gmail_then_a_user_just_enters_their_address(client, db, sends):
    csrf = _login(client, "admin", PASSWORD)
    page = client.get("/settings").text
    assert "Outgoing email" in page and "Send mail as" in page and "app password" in page
    r = client.post("/admin/mail", data={**GMAIL, "csrf_token": csrf, "action": "test"})
    assert r.status_code == 200 and mailserver.load(db).from_addr == "pitwall@example.org"
    # No alert address yet, so the test goes to the From address.
    assert _q(sends[-1][1][0])["to"] == "pitwall@example.org"
    page = client.get("/settings").text
    assert "abcdefghijklmnop" not in page and "sending as" in page

    create_user(db, "friend", "a long password 1")
    db.commit()
    csrf = _login(client, "friend", "a long password 1")
    page = client.get("/settings").text
    assert "Send my email alerts to" in page and 'name="password"' not in page
    r = client.post(
        "/settings/email",
        data={"csrf_token": csrf, "notify_email": "friend@example.com", "action": "test"},
    )
    assert "Test email handed to the mail server for friend@example.com" in r.text
    assert _q(sends[-1][1][0])["to"] == "friend@example.com"
    friend = db.query(User).filter_by(username="friend").one()
    db.expire_all()
    assert load_settings(db, friend.id).notify_email == "friend@example.com"
    r = client.post("/settings/email", data={"csrf_token": csrf, "notify_email": "not an email"})
    assert "Couldn&#39;t save your address" in r.text or "Couldn't save your address" in r.text


def test_one_click_conversion_of_an_existing_channel(client, db):
    built = targets_svc.build(
        "email",
        "gmail",
        {"address": "me@gmail.com", "password": "abcdefghijklmnop", "to_addr": "me@x.org"},
    )
    db.add(
        NotificationTarget(user_id=1, channel="email", kind="gmail", label="Gmail", url=built.url)
    )
    db.commit()
    target_id = db.query(NotificationTarget).one().id
    csrf = _login(client, "admin", PASSWORD)
    assert "Use for everyone" in client.get("/settings").text
    client.post(f"/admin/mail/from-target/{target_id}", data={"csrf_token": csrf})
    db.expire_all()
    server = mailserver.load(db)
    assert server.host == "smtp.gmail.com" and server.username == "me@gmail.com"
    assert db.query(NotificationTarget).count() == 0
    assert load_settings(db, 1).notify_email == "me@x.org"
    urls = targets_svc.channel_urls("email", db, 1)
    assert sum("me%40x.org" in u or "me@x.org" in u for u in urls) == 1  # exactly once


def test_admin_can_set_a_new_users_email(client, db):
    csrf = _login(client, "admin", PASSWORD)
    client.post(
        "/admin/users",
        data={
            "csrf_token": csrf,
            "username": "pal",
            "email": "pal@example.com",
            "must_change": "1",
        },
    )
    pal = db.query(User).filter_by(username="pal").one()
    assert load_settings(db, pal.id).notify_email == "pal@example.com"
    assert "pal@example.com" in client.get("/admin/users").text
    r = client.post("/admin/users", data={"csrf_token": csrf, "username": "pal2", "email": "nope"})
    assert r.status_code == 400 and not db.query(User).filter_by(username="pal2").count()


def test_invitees_can_give_their_email(client, db):
    mailserver.save(db, GMAIL)
    token = create_invite(db, db.get(User, 1))
    db.commit()
    page = client.get(f"/invite/{token}").text
    assert 'name="email"' in page
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    client.post(
        f"/invite/{token}",
        data={
            "csrf_token": csrf,
            "username": "newbie",
            "password": "a long password 9",
            "password2": "a long password 9",
            "email": "newbie@example.com",
        },
    )
    newbie = db.query(User).filter_by(username="newbie").one()
    assert load_settings(db, newbie.id).notify_email == "newbie@example.com"


# ---------------------------------------------------------------------------- delivery problems


def test_email_only_users_dont_log_webhook_failures(db):
    from pitwall.models import NotificationLog, Outbox
    from pitwall.notify import outbox
    from pitwall.notify.message import Message

    friend = create_user(db, "friend", "a long password 1")
    mailserver.save(db, GMAIL)
    mailserver.set_user_addresses(db, friend.id, ["friend@example.com"])
    db.flush()
    row = outbox.enqueue(db, Message(type="results", title="Results", text="P1", user_id=friend.id))
    assert set(row.channels.split(",")) == {"email", "webhook"}
    calls = []

    def sender(channel, msg):
        calls.append(channel)
        return (True, "delivered") if channel == "email" else (None, "no webhook destination")

    stats = outbox.flush(db, sender=sender)
    assert calls == ["email", "webhook"] and stats["sent"] == 1 and stats["failed"] == 0
    assert db.get(Outbox, row.id).status == "sent"  # not retried, not marked failed
    logs = db.query(NotificationLog).filter_by(user_id=friend.id).all()
    assert [(log.channel, log.status) for log in logs] == [("email", "sent")]


def test_personal_gmail_sending_as_another_domain_is_flagged(db):
    risky = mailserver.save(db, GMAIL)  # me@gmail.com sending as pitwall@example.org
    assert mailserver.spoofing_risk(risky)
    assert not mailserver.spoofing_risk(mailserver.save(db, {**GMAIL, "from_addr": "me@gmail.com"}))
    workspace = mailserver.save(
        db, {**GMAIL, "username": "f1@example.org", "from_addr": "pitwall@example.org"}
    )
    assert not mailserver.spoofing_risk(workspace)  # Google Workspace on that domain


def test_admin_sees_the_warning_and_can_test_any_address(client, db, sends):
    csrf = _login(client, "admin", PASSWORD)
    client.post("/admin/mail", data={**GMAIL, "csrf_token": csrf})
    page = client.get("/settings").text
    assert "may be rejected or land in spam" in page and "Everyone's notifications" in page
    r = client.post(
        "/admin/mail",
        data={**GMAIL, "csrf_token": csrf, "action": "test", "test_to": "pal@example.net"},
    )
    assert _q(sends[-1][1][0])["to"] == "pal@example.net"
    assert "bounce message" in r.text
    bad = client.post(
        "/admin/mail",
        data={**GMAIL, "csrf_token": csrf, "action": "test", "test_to": "nope"},
    )
    assert "no test was sent" in bad.text
