"""Multi-user: accounts, invites, isolation, per-user alerts and internet hardening."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall import jobs, users
from pitwall.models import (
    OWNER_ID,
    Event,
    Invite,
    NotificationTarget,
    Outbox,
    User,
)
from pitwall.notify import outbox
from pitwall.notify.message import Message
from pitwall.services import reminders
from pitwall.services.sync import apply_schedule
from pitwall.settings_store import load_settings, save_settings
from pitwall.web import auth
from tests.conftest import dt, races

ADMIN_PW = "correct horse battery"
BOB_PW = "correct-staple-42x"
NOW = dt("2025-03-01T12:00:00")


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def csrf(c, path="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text).group(1)


def login(c, username="admin", password=ADMIN_PW, expect=303):
    r = c.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf(c)},
        follow_redirects=False,
    )
    assert r.status_code == expect, r.text[:300]
    return r


@pytest.fixture
def bob(db):
    user = users.create_user(db, "bob", BOB_PW)
    db.commit()
    return user


# ---------------------------------------------------------------------------- accounts


def test_password_hashing_and_rules():
    h = users.hash_password("a-good-password")
    assert h.startswith("scrypt$") and users.verify_password("a-good-password", h)
    assert not users.verify_password("wrong", h) and not users.verify_password("x", "!")
    for bad in ("short", "aaaaaaaaaaaa", "bob-is-my-password"):
        with pytest.raises(users.AccountError):
            users.check_password(bad, "bob")


def test_owner_bootstrap_from_env(db):
    users.bootstrap(db)
    owner = db.get(User, OWNER_ID)
    assert owner.username == "admin" and owner.is_admin
    assert users.verify_password(ADMIN_PW, owner.password_hash)
    assert owner.ics_token == "ics-test-token-123"  # existing calendar links keep working


def test_upgrade_assigns_existing_data_to_admin(tmp_path):
    """A database from before multi-user support keeps its data, now owned by `admin`."""
    from alembic import command

    from pitwall.db import make_engine
    from pitwall.migrate import alembic_config

    eng = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    cfg = alembic_config()
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0004")
        conn.exec_driver_sql(
            "INSERT INTO driver (id, given_name, family_name) "
            "VALUES ('leclerc', 'Charles', 'Leclerc')"
        )
        conn.exec_driver_sql("INSERT INTO followed_driver (driver_id) VALUES ('leclerc')")
        conn.exec_driver_sql(
            "INSERT INTO setting (key, value) VALUES "
            "('timezone', '\"Europe/London\"'), ('backfill_progress', '{}')"
        )
        conn.exec_driver_sql(
            "INSERT INTO notification_target (channel, kind, label, url, "
            "enabled, created_at) VALUES ('webhook', 'ntfy', 'x', 'ntfy://t', 1, "
            "'2025-01-01 00:00:00')"
        )
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        assert conn.exec_driver_sql('SELECT id, username, is_admin FROM "user"').all() == [
            (1, "admin", 1)
        ]
        assert conn.exec_driver_sql("SELECT user_id, driver_id FROM followed_driver").all() == [
            (1, "leclerc")
        ]
        assert conn.exec_driver_sql("SELECT user_id, key FROM user_setting").all() == [
            (1, "timezone")
        ]
        assert conn.exec_driver_sql("SELECT key FROM setting").all() == [("backfill_progress",)]
        assert conn.exec_driver_sql("SELECT user_id FROM notification_target").all() == [(1,)]
    eng.dispose()


def test_login_by_username_and_lockout(client, db, bob):
    login(client, "bob", "wrong-password-x", expect=401)
    for _ in range(users.LOCKOUT_FAILURES - 1):
        auth.limiter._failures.clear()  # isolate the per-account lock from the per-IP limit
        login(client, "bob", "wrong-password-x", expect=401)
    auth.limiter._failures.clear()
    r = login(client, "bob", BOB_PW, expect=401)  # locked, even with the right password
    assert "temporarily locked" in r.text
    r2 = login(client, "nobody", "whatever-123", expect=401)  # same message for unknown users
    assert r2.text.count("Wrong username or password") == 1
    db.expire_all()
    with time_machine.travel(datetime.now(UTC) + timedelta(minutes=16), tick=False):
        auth.limiter._failures.clear()
        login(client, "BOB", BOB_PW)  # usernames are case-insensitive


def test_invite_flow_and_admin_only_pages(client, db):
    login(client)
    r = client.post(
        "/admin/invites", data={"csrf_token": csrf(client, "/admin/users")}, follow_redirects=True
    )
    link = re.search(r'id="new-link">([^<]+)<', r.text).group(1)
    token = link.rsplit("/", 1)[1]
    assert db.scalars(select(Invite)).one().token_hash != token  # only a hash is stored
    client.post("/logout", data={"csrf_token": csrf(client, "/")})

    page = client.get(f"/invite/{token}").text
    assert "Create your account" in page
    r = client.post(
        f"/invite/{token}",
        data={
            "csrf_token": csrf(client, f"/invite/{token}"),
            "username": "Carol",
            "password": "staple-battery-77",
            "password2": "staple-battery-77",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "carol" in client.get("/account").text  # logged in as the new user
    assert client.get("/admin/users").status_code == 403  # not an admin
    assert (
        "invalid" in client.get(f"/invite/{token}").text
        or "expired or was already used" in client.get(f"/invite/{token}").text
    )


def test_password_reset_link_and_session_invalidation(client, db, bob):
    other = TestClient(client.app)
    login(other, "bob", BOB_PW)
    assert other.get("/", follow_redirects=False).status_code == 200
    admin = db.get(User, OWNER_ID)
    users.bootstrap(db)
    token = users.create_invite(db, admin, reset_user=db.get(User, bob.id))
    db.commit()
    assert "Choose a new password for bob" in client.get(f"/invite/{token}").text
    r = client.post(
        f"/invite/{token}",
        data={
            "csrf_token": csrf(client, f"/invite/{token}"),
            "password": "new-staple-battery-3",
            "password2": "new-staple-battery-3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert other.get("/", follow_redirects=False).status_code == 303  # old session is gone


def test_admin_safety_rails(client, db, bob):
    login(client)
    token = csrf(client, "/admin/users")
    client.post(f"/admin/users/{OWNER_ID}/delete", data={"csrf_token": token})
    client.post(f"/admin/users/{OWNER_ID}/toggle-admin", data={"csrf_token": token})
    client.post(f"/admin/users/{OWNER_ID}/toggle-active", data={"csrf_token": token})
    db.expire_all()
    owner = db.get(User, OWNER_ID)
    assert owner is not None and owner.is_admin and owner.active
    client.post(f"/admin/users/{bob.id}/toggle-active", data={"csrf_token": token})
    db.expire_all()
    assert not db.get(User, bob.id).active
    auth.limiter._failures.clear()
    login(TestClient(client.app), "bob", BOB_PW, expect=401)  # disabled accounts can't log in
    client.post(f"/admin/users/{bob.id}/delete", data={"csrf_token": token})
    db.expire_all()
    assert db.scalars(select(User).where(User.username == "bob")).first() is None


# ---------------------------------------------------------------------------- isolation


def test_users_have_separate_settings_channels_and_drivers(client, db, bob):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    db.add(
        NotificationTarget(
            user_id=OWNER_ID,
            channel="webhook",
            kind="ntfy",
            label="Admin phone",
            url="ntfy://admin-topic",
        )
    )
    db.commit()
    login(client, "bob", BOB_PW)
    page = client.get("/settings").text
    assert "Admin phone" not in page and "mailtos://smtp.example.com" not in page  # .env = admin
    admin_target = db.scalars(select(NotificationTarget)).one()
    r = client.post(
        f"/settings/targets/{admin_target.id}/delete",
        data={"csrf_token": csrf(client, "/settings")},
    )
    assert r.status_code == 404  # can't touch someone else's channel
    form = {
        "csrf_token": csrf(client, "/settings"),
        "timezone": "Asia/Tokyo",
        "offset_RACE": "30m",
        "odd_start": "00:00",
        "odd_end": "07:00",
        "quiet_start": "23:00",
        "quiet_end": "07:00",
        "digest_day": "mon",
        "digest_time": "08:00",
        "backfill_start_year": "1990",
    }
    client.post("/settings", data=form)
    db.expire_all()
    assert load_settings(db, bob.id).timezone == "Asia/Tokyo"
    assert load_settings(db, OWNER_ID).timezone == "America/New_York"
    assert load_settings(db).backfill_start_year == 2000  # system setting: admins only


def test_calendar_feed_is_per_user(client, db, bob):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    save_settings(db, {"timezone": "Asia/Tokyo"}, bob.id)
    db.commit()
    mine = client.get(f"/calendar.ics?token={bob.ics_token}&sessions=race").text
    assert "JST" in mine.replace("\r\n ", "")
    assert client.get("/calendar.ics?token=ics-test-token-123").status_code == 200  # admin's
    assert client.get("/calendar.ics?token=short").status_code == 404


# ---------------------------------------------------------------------------- alerts


def test_alerts_fan_out_with_each_users_timezone(db, api, bob):
    routes, factory = api
    save_settings(db, {"timezone": "Asia/Tokyo"}, bob.id)
    db.commit()
    routes.set("2025.json", "schedule_2025.json")
    jobs.sync_schedule(factory(), 2025, NOW)
    from tests.conftest import load

    moved = load("schedule_2025.json")
    moved["MRData"]["RaceTable"]["Races"][2]["time"] = "21:00:00Z"
    routes.set("2025.json", moved)
    jobs.sync_schedule(factory(), 2025, NOW)
    db.expire_all()
    rows = {
        r.user_id: r for r in db.scalars(select(Outbox).where(Outbox.type == "schedule_change"))
    }
    assert set(rows) == {OWNER_ID, bob.id}
    assert "EDT" in rows[OWNER_ID].text and "JST" in rows[bob.id].text


def test_reminder_offsets_are_per_user(db, bob, sent):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    offsets = load_settings(db, bob.id).reminder_offsets
    offsets.update({"RACE": [30], "Q": []})
    save_settings(db, {"reminder_offsets": offsets}, bob.id)
    db.commit()
    plan = reminders.plan(db, None, NOW)
    race = db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one().sessions
    race = next(s for s in race if s.type == "RACE")
    assert {p.offset_minutes for p in plan.values() if p.session_id == race.id} == {1440, 60, 30}
    with time_machine.travel(race.start_utc - timedelta(minutes=30), tick=False):
        jobs.send_reminder_job(race.id, 30, (race.start_utc - timedelta(minutes=30)).isoformat())
    db.expire_all()
    assert [r.user_id for r in db.scalars(select(Outbox))] == [bob.id]  # only bob asked for 30m


def test_spoiler_and_quiet_hours_are_per_user(db, bob, sent):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    ev = db.scalars(select(Event)).first()
    save_settings(db, {"spoiler_enabled": True})  # admin only
    for uid in (OWNER_ID, bob.id):
        outbox.enqueue(
            db,
            Message(type="results", title=f"R{uid}", text="P1", event_id=ev.id, user_id=uid),
            now=NOW,
        )
    outbox.flush(db, now=NOW)
    assert sent.titles() == [f"R{bob.id}"]
    outbox.mark_watched(db, ev.id, user_id=OWNER_ID)
    outbox.flush(db, now=NOW)
    assert sent.titles() == [f"R{bob.id}", f"R{OWNER_ID}"]
    assert [m.user_id for _c, m in sent.items] == [bob.id, OWNER_ID]  # each to their own channels


def test_preview_goes_out_at_9am_in_each_users_timezone(db, bob, sent):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    save_settings(db, {"timezone": "Asia/Tokyo"}, bob.id)
    save_settings(db, {"routing": {"preview": ["webhook"]}}, bob.id)
    db.commit()
    monday_tokyo_9 = dt("2025-10-20T00:00:00")  # 09:00 Tokyo, Sunday 20:00 New York
    with time_machine.travel(monday_tokyo_9, tick=False):
        jobs.personal_job(monday_tokyo_9)
        jobs.personal_job(monday_tokyo_9)
    previews = list(db.scalars(select(Outbox).where(Outbox.type == "preview")))
    assert [p.user_id for p in previews] == [bob.id]
    monday_ny_9 = dt("2025-10-20T13:00:00")
    with time_machine.travel(monday_ny_9, tick=False):
        jobs.personal_job(monday_ny_9)
    db.expire_all()
    assert sorted(
        p.user_id for p in db.scalars(select(Outbox).where(Outbox.type == "preview"))
    ) == [OWNER_ID, bob.id]


def test_digest_runs_on_each_users_schedule(db, bob, sent):
    save_settings(
        db,
        {
            "digest_enabled": True,
            "digest_day": "sun",
            "digest_time": "18:00",
            "routing": {"results": ["webhook"], "digest": ["webhook"]},
        },
        bob.id,
    )
    db.commit()
    first = dt("2025-03-05T12:00:00")
    assert not jobs.maybe_send_digest(bob.id, first)  # just enabled: wait for the next slot
    outbox.enqueue(db, Message(type="results", title="R", text="P1", user_id=bob.id), now=first)
    outbox.flush(db, now=first)
    db.commit()
    before = dt("2025-03-09T21:59:00")  # Sunday 17:59 New York (EDT)
    assert not jobs.maybe_send_digest(bob.id, before)
    after = dt("2025-03-09T22:01:00")
    with time_machine.travel(after, tick=False):
        assert jobs.maybe_send_digest(bob.id, after)
    assert any(t.startswith("🗞️") for t in sent.titles())
    assert not jobs.maybe_send_digest(bob.id, after + timedelta(minutes=5))


# ---------------------------------------------------------------------------- hardening


def test_security_headers(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "onchange=" not in client.get("/login").text


def _behind_proxy(peer: str):
    """The app as `python -m pitwall serve` runs it, trusting only the proxy machine
    192.168.1.20 for X-Forwarded-For, with requests arriving from `peer`."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from pitwall.web.app import create_app

    app = ProxyHeadersMiddleware(
        create_app(manage_db=False, start_scheduler=False), trusted_hosts="192.168.1.20"
    )
    return TestClient(app, client=(peer, 50000))


def _fail_logins(c, forwarded_for: str) -> None:
    for _ in range(auth.MAX_FAILURES):  # unknown user: tests the per-IP limit, not the lockout
        c.post(
            "/login",
            data={"username": "mallory", "password": "nope-nope-nope", "csrf_token": csrf(c)},
            headers={"x-forwarded-for": forwarded_for},
        )


def test_real_visitor_ip_from_trusted_proxy_used_for_rate_limiting(client):
    with _behind_proxy("192.168.1.20") as c:
        _fail_logins(c, "198.51.100.7")
        blocked = c.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PW, "csrf_token": csrf(c)},
            headers={"x-forwarded-for": "198.51.100.7"},
        )
        assert blocked.status_code == 429
        # A different visitor through the same proxy isn't blocked by that person's failures.
        ok = c.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PW, "csrf_token": csrf(c)},
            headers={"x-forwarded-for": "203.0.113.50"},
            follow_redirects=False,
        )
        assert ok.status_code == 303


def test_forwarded_ip_from_untrusted_machine_is_ignored(client):
    with _behind_proxy("192.168.1.99") as c:
        _fail_logins(c, "198.51.100.7")
        # Changing the header doesn't get around the limit: the machine's own IP is used.
        dodge = c.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PW, "csrf_token": csrf(c)},
            headers={"x-forwarded-for": "203.0.113.50"},
        )
        assert dodge.status_code == 429


def test_login_cookie_secure_only_for_https_visits(client):
    def login_cookie(c, headers):
        r = c.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PW, "csrf_token": csrf(c)},
            headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "strict-transport-security" not in r.headers  # would break http on the LAN
        return next(v for v in r.headers.get_list("set-cookie") if "pitwall_session=" in v)

    with _behind_proxy("192.168.1.20") as c:  # through the proxy, which says it was HTTPS
        cookie = login_cookie(c, {"x-forwarded-proto": "https", "x-forwarded-for": "1.2.3.4"})
        assert "; secure" in cookie.lower() and "httponly" in cookie.lower()
    with _behind_proxy("192.168.1.99") as c:  # LAN over plain http, even if it claims https
        cookie = login_cookie(c, {"x-forwarded-proto": "https"})
        assert "secure" not in cookie.lower()
