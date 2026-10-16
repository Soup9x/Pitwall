"""Step 3 UI: theme preference, PWA files, self-hosted assets, no inline scripts, race-weekend
mode, timeline states, freshness badges and the email layout."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, SourceStatus
from pitwall.services import freshness
from pitwall.services.sync import apply_schedule, apply_standings
from pitwall.settings_store import load_settings
from pitwall.web import auth, dashboard
from tests.conftest import dt, load, races


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _login(c):
    c.cookies.clear()
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
    c.post(
        "/login",
        data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
    )
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text)[1]


@pytest.fixture
def mexico(db):
    apply_schedule(
        db,
        2025,
        races("schedule_2025.json"),
        now=dt("2025-03-01T00:00:00"),
        curated={"rodriguez": {"timezone": "America/Mexico_City"}},
    )
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def test_theme_preference_is_per_user_and_server_rendered(client, db):
    token = _login(client)
    assert 'data-theme="dark"' in client.get("/").text  # dark by default
    assert "/prefs/theme" not in client.get("/").text  # no theme buttons in the header
    account = client.get("/account").text
    assert 'action="/prefs/theme"' in account and 'value="dark" checked' in account
    client.post("/prefs/theme", data={"csrf_token": token, "theme": "light", "next": "/news"})
    assert 'data-theme="light"' in client.get("/news").text
    assert 'value="light" checked' in client.get("/account").text
    client.post("/prefs/theme", data={"csrf_token": token, "theme": "neon"})
    assert load_settings(db).theme == "light"  # unknown values are ignored
    r = client.post(
        "/prefs/theme",
        data={"csrf_token": token, "theme": "system", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"  # no open redirect


def test_pwa_files_and_self_hosted_assets(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert "Pages are never cached" in sw.text
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and len(manifest["icons"]) >= 4
    for path in (
        "/static/fonts/fonts.css",
        "/static/fonts/titillium-web-latin-400-normal.woff2",
        "/static/fonts/jetbrains-mono-latin-400-normal.woff2",
        "/static/icons/logo.svg",
        "/static/icons/icon-512.png",
        "/static/offline.html",
        "/static/vendor/chart.umd.min.js",
    ):
        assert client.get(path).status_code == 200, path
    css = client.get("/static/style.css").text
    assert "prefers-reduced-motion" in css and "prefers-color-scheme: light" in css
    assert "googleapis" not in css and "http" not in client.get("/static/fonts/fonts.css").text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/events",
        "/circuits",
        "/circuits/rodriguez",
        "/standings",
        "/news",
        "/settings",
        "/account",
        "/login",
    ],
)
def test_no_inline_scripts_or_external_resources(client, mexico, path):
    if path != "/login":
        _login(client)
    html = client.get(path).text
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag or 'type="application/json"' in tag, tag
    assert not re.search(r'(src|href)="https?://(?!www\.fia\.com)', html.split("<main")[0])
    assert " on" not in re.sub(r"<script.*?</script>", "", html, flags=re.S).split(">")[0]


def test_race_weekend_window_is_track_local(db, mexico):
    # Mexico City race: Sunday 26 Oct 2025, 20:00 UTC (14:00 local).
    thursday_start = datetime(2025, 10, 23, 6, 0, tzinfo=UTC)  # 00:00 in Mexico City
    assert dashboard.race_weekend(mexico, thursday_start)
    assert not dashboard.race_weekend(mexico, thursday_start - timedelta(minutes=1))
    race = next(s for s in mexico.sessions if s.type == "RACE")
    assert dashboard.race_weekend(mexico, race.end_utc_est)
    assert not dashboard.race_weekend(mexico, race.end_utc_est + timedelta(minutes=1))


def test_timeline_states(db, mexico):
    settings = load_settings(db)
    during_fp2 = datetime(2025, 10, 24, 22, 30, tzinfo=UTC)
    items = {i.view.type: i for i in dashboard.timeline(mexico, settings, during_fp2)}
    assert items["FP1"].state == "done" and items["FP2"].state == "live"
    assert items["FP3"].state == "later" and items["RACE"].state == "later"
    before = {
        i.view.type: i
        for i in dashboard.timeline(mexico, settings, during_fp2 - timedelta(hours=1))
    }
    assert before["FP2"].state == "next" and before["FP2"].marker == "▶"


def _order(html, *labels):
    return [html.index(label) for label in labels]


def test_dashboard_reorders_on_race_weekend(client, db, mexico):
    with time_machine.travel(datetime(2025, 10, 24, 12, tzinfo=UTC), tick=False):
        _login(client)
        weekend = client.get("/").text
    pos = _order(
        weekend,
        'aria-label="Weekend sessions"',
        "<h2>Penalties",
        "<h2>Weather",
        "<h2>Tyres",
        "<h2>Drivers",
    )
    assert pos == sorted(pos)
    assert "race weekend" in weekend and 'data-title="Practice 1"' in weekend
    with time_machine.travel(datetime(2025, 10, 13, 12, tzinfo=UTC), tick=False):
        _login(client)
        normal = client.get("/").text
    assert normal.index("<h2>Drivers") < normal.index('aria-label="Weekend sessions"')
    assert "<h2>Penalties" not in normal
    # The timing tower uses 3-letter codes and arrows with words for screen readers.
    assert 'class="t-drv stripe"' in weekend


def test_freshness_states(db):
    now = datetime(2025, 10, 1, 12, tzinfo=UTC)
    assert freshness.of(db, "jolpica", now).state == "never"
    db.add(SourceStatus(source="jolpica", last_success_at=now - timedelta(minutes=12)))
    db.flush()
    f = freshness.of(db, "jolpica", now)
    assert (f.state, f.text, f.symbol) == ("ok", "Synced 12m ago", "●")
    assert freshness.of(db, "jolpica", now + timedelta(hours=13)).state == "stale"
    db.get(SourceStatus, "jolpica").last_error_at = now - timedelta(minutes=1)
    db.flush()
    f = freshness.of(db, "jolpica", now)
    assert f.state == "failed" and f.symbol == "✕" and f.text.startswith("Last sync failed")


def test_email_layout(db, mexico):
    from pitwall.notify import builders

    msg = builders.preview(db, mexico, load_settings(db), datetime(2025, 10, 20, tzinfo=UTC))
    assert 'width="600"' in msg.html and "max-width:600px" in msg.html
    assert "background:#F6F7F9" in msg.html and "<script" not in msg.html
    assert msg.text and "<" not in msg.text.splitlines()[0]


def test_static_files_are_versioned_so_updates_reach_browsers(client):
    _login(client)
    page = client.get("/").text
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{12})"', page)
    assert css and re.search(r'src="/static/app\.js\?v=[0-9a-f]{12}"', page)
    versioned = client.get(css[1])
    assert versioned.status_code == 200 and "immutable" in versioned.headers["cache-control"]
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"
    assert 'class="sr-only">Account' not in page and 'aria-label="Account (admin)"' in page
