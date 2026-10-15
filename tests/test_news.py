"""RSS news: headline-only storage, tagging, track-change alerts and spoiler hiding.

The feed below is hand-written RSS 2.0 for the tests, not a copy of a real feed."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, NewsFeed, NewsItem, Outbox
from pitwall.services import news
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import save_settings
from pitwall.web import auth
from tests.conftest import dt, load, races

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test F1 news</title>
<item><title>Mexico City track resurfaced ahead of this weekend</title>
  <link>https://news.example/resurface</link><pubDate>Mon, 20 Oct 2025 09:00:00 GMT</pubDate>
  <description>Full article text that must never be stored.</description></item>
<item><title>Norris extends McLaren deal</title>
  <link>https://news.example/norris</link><pubDate>Tue, 21 Oct 2025 12:00:00 GMT</pubDate></item>
<item><title>Qualifying report: pole decided in Mexico City</title>
  <link>https://news.example/quali</link><pubDate>Sat, 25 Oct 2025 22:30:00 GMT</pubDate></item>
<item><title>No link here</title></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""

NOW = datetime(2025, 10, 22, tzinfo=UTC)


@pytest.fixture
def world(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", load(f)["MRData"]["StandingsTable"]["StandingsLists"][0])
    apply_standings(
        db,
        "constructor",
        load("constructor_standings_2025_3.json")["MRData"]["StandingsTable"]["StandingsLists"][0],
    )
    db.add(NewsFeed(name="Test feed", url="https://news.example/feed.xml", enabled=True))
    from pitwall.settings_store import set_kv

    set_kv(db, news.SEEDED_KEY, True)  # only the test feed
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def _client(responses):
    calls = []

    def handler(req):
        calls.append(dict(req.headers))
        r = responses.pop(0) if len(responses) > 1 else responses[0]
        return r() if callable(r) else r

    c = news.make_http_client(transport=httpx.MockTransport(handler))
    c.calls = calls
    return c


def ok(body=FEED, etag='"v1"'):
    return httpx.Response(200, content=body.encode(), headers={"ETag": etag})


def test_parse_feed_keeps_only_headline_fields():
    items = news.parse_feed(FEED.encode())
    assert [i.link for i in items] == [
        "https://news.example/resurface",
        "https://news.example/norris",
        "https://news.example/quali",
    ]
    assert items[0].published_at == datetime(2025, 10, 20, 9, tzinfo=UTC)
    assert set(vars(items[0])) == {"title", "link", "published_at"}


def test_sync_tags_and_dedups(db, world):
    new = news.sync(_client([ok()]), NOW)
    assert len(new) == 3
    db.expire_all()
    rows = {i.link: i for i in db.scalars(select(NewsItem))}
    assert not any("Full article text" in json.dumps(vars(r), default=str) for r in rows.values())
    resurface = json.loads(rows["https://news.example/resurface"].tags_json)
    assert "track" in resurface and "circuit:rodriguez" in resurface
    norris = json.loads(rows["https://news.example/norris"].tags_json)
    assert "driver:norris" in norris and "team:mclaren" in norris
    # A second poll stores nothing new, and sends the ETag back.
    client = _client([httpx.Response(304)])
    assert news.sync(client, NOW + timedelta(minutes=30)) == []
    assert client.calls[0].get("if-none-match") == '"v1"'


def test_failing_feed_is_recorded_and_others_continue(db, world):
    db.add(NewsFeed(name="Broken", url="https://broken.example/rss", enabled=True))
    db.commit()

    def handler(req):
        if "broken" in str(req.url):
            return httpx.Response(500)
        return ok()

    client = news.make_http_client(transport=httpx.MockTransport(handler))
    assert len(news.sync(client, NOW)) == 3
    db.expire_all()
    broken = db.scalars(select(NewsFeed).where(NewsFeed.name == "Broken")).one()
    assert broken.last_error and broken.last_success_at is None
    html_page = news.sync(
        news.make_http_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>hi</html>"))
        ),
        NOW,
    )
    assert html_page == []


def test_track_news_alert_only_for_the_upcoming_circuit(db, world, sent):
    from pitwall import jobs

    save_settings(db, {"routing": {"track_news": ["webhook"]}})
    db.commit()
    jobs.set_news_factory(lambda: _client([ok()]))
    jobs.news_job(NOW)
    db.expire_all()
    titles = [t for _c, m in sent.items for t in [m.title]]
    assert titles == [
        "🛠️ Track news for the Mexico City Grand Prix: Mexico City track resurfaced ahead of "
        "this weekend"
    ]


def test_no_alert_for_an_old_backlog(db, world, sent):
    from pitwall import jobs

    save_settings(db, {"routing": {"track_news": ["webhook"]}})
    db.commit()
    jobs.set_news_factory(lambda: _client([ok()]))
    jobs.news_job(NOW + timedelta(days=4))  # the resurfacing story is 6 days old by then
    assert sent.items == []
    assert db.scalars(select(Outbox)).first() is None


def test_spoiler_mode_hides_headlines_after_the_session_started(db, world):
    news.sync(_client([ok()]), datetime(2025, 10, 25, 23, tzinfo=UTC))
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    from pitwall.settings_store import load_settings

    settings = load_settings(db)
    during = datetime(2025, 10, 26, 10, tzinfo=UTC)  # after qualifying, race not watched
    view = news.visible(db, 1, settings, now=during)
    assert view.hidden == 1 and view.held_event.slug == "2025-rodriguez"
    assert "https://news.example/quali" not in [i.link for i in view.items]
    assert len(news.visible(db, 1, settings, now=during, reveal=True).items) == 3
    # Once results are in and the event is watched, everything shows.
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=during)
    from pitwall.services import spoilers

    spoilers.mark_watched(db, world.id, user_id=1)
    assert news.visible(db, 1, settings, now=during).hidden == 0


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
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/settings").text)[1]


def test_pages(db, world, client):
    news.sync(_client([ok()]), datetime(2025, 10, 25, 23, tzinfo=UTC))
    from pitwall.models import FollowedDriver

    db.add(FollowedDriver(driver_id="norris"))
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    with time_machine.travel(datetime(2025, 10, 26, 10, tzinfo=UTC), tick=False):
        _login(client)
        dash = client.get("/").text
        all_news = client.get("/news").text
        followed = client.get("/news?show=followed").text
        track = client.get("/news?show=track").text
        event_page = client.get(f"/events/{world.id}").text
    assert "Norris extends McLaren deal" in dash and "★ followed" in dash
    assert "1 headline hidden by spoiler mode" in all_news
    assert "Qualifying report" not in all_news and "Qualifying report" not in dash
    assert "Norris extends" in followed and "resurfaced" not in followed
    assert "resurfaced" in track and "Norris extends" not in track
    assert "Track updates" in event_page and "resurfaced" in event_page
    assert "Full article text" not in all_news


def test_admin_feed_settings(db, world, client):
    with time_machine.travel(NOW, tick=False):
        token = _login(client)
        r = client.post(
            "/admin/news/feeds",
            data={"csrf_token": token, "name": "Extra", "url": "ftp://nope"},
            follow_redirects=True,
        )
        assert "Feed not added" in r.text
        client.post(
            "/admin/news/feeds",
            data={"csrf_token": token, "name": "Extra", "url": "https://extra.example/rss"},
        )
        client.post(
            "/admin/news/keywords",
            data={"csrf_token": token, "keywords": "pit lane\nresurface\nRESURFACE"},
        )
        page = client.get("/settings").text
    assert "https://extra.example/rss" in page
    assert news.track_keywords(db) == ["pit lane", "resurface"]
    extra = db.scalars(select(NewsFeed).where(NewsFeed.name == "Extra")).one()
    with time_machine.travel(NOW, tick=False):
        token = _login(client)
        client.post(f"/admin/news/feeds/{extra.id}/toggle", data={"csrf_token": token})
    db.expire_all()
    assert db.get(NewsFeed, extra.id).enabled is False


def test_race_week_preview_has_track_updates_and_tyres(db, world):
    from pitwall.notify import builders
    from pitwall.services import tyres
    from pitwall.settings_store import load_settings

    news.sync(_client([ok()]), NOW)
    tyres.set_manual(db, world.id, "C3", "C4", "C5", user_id=1)
    msg = builders.preview(db, world, load_settings(db), NOW)
    assert "TRACK UPDATES" in msg.text and "Mexico City track resurfaced" in msg.text
    assert "Norris extends" not in msg.text  # only track-change news about this circuit
    assert "Hard C3, medium C4, soft C5" in msg.text
    assert "Track updates" in msg.html and "Hard <strong>C3</strong>" in msg.html
