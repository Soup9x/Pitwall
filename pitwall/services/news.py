"""RSS/Atom news headlines.

Only the title, link, source, date and tags of each item are stored, never article text.
Items are tagged at fetch time:

* driver:<id>   - names a driver from the current season
* team:<id>     - names a team from the current season
* circuit:<id>  - names the upcoming event's circuit, city, country or Grand Prix
* track         - mentions a track-change keyword (resurfacing, layout, kerbs, ...)

A headline tagged both `track` and the upcoming circuit raises a "track_news" alert. While
spoiler mode holds an event, headlines published after its first competitive session started
are hidden (and counted)."""

from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall import USER_AGENT
from pitwall.db import session_scope, utcnow
from pitwall.models import (
    Constructor,
    Driver,
    Event,
    NewsFeed,
    NewsItem,
    Result,
    Session,
    StandingsSnapshot,
)
from pitwall.notify.message import Message
from pitwall.services import spoilers
from pitwall.services.presenters import current_or_next_event, event_url
from pitwall.settings_store import AppSettings, get_kv, set_kv

log = logging.getLogger(__name__)

SEED_FEEDS = (
    ("Formula 1", "https://www.formula1.com/en/latest/all.xml"),
    ("Autosport", "https://www.autosport.com/rss/f1/news/"),
    ("The Race", "https://www.the-race.com/category/formula-1/feed/"),
    ("Motorsport.com", "https://www.motorsport.com/rss/f1/news/"),
)
SEEDED_KEY = "news_feeds_seeded"
KEYWORDS_KEY = "news_track_keywords"
DEFAULT_TRACK_KEYWORDS = (
    "resurface",
    "resurfaced",
    "resurfacing",
    "layout",
    "kerb",
    "kerbs",
    "DRS zone",
    "track limits",
    "chicane",
    "asphalt",
)
KEEP_DAYS = 60
ALERT_MAX_AGE = timedelta(days=3)  # don't alert on an old backlog the first time a feed loads
MAX_FEED_BYTES = 5_000_000
_TEAM_SUFFIXES = re.compile(r"\s+(f1 team|formula one team|racing|team)$", re.I)


# ---------------------------------------------------------------------------- feeds


def seed_feeds(db: DB) -> None:
    """Add the default feeds once (an admin can remove or disable them afterwards)."""
    if get_kv(db, SEEDED_KEY):
        return
    have = set(db.scalars(select(NewsFeed.url)))
    for name, url in SEED_FEEDS:
        if url not in have:
            db.add(NewsFeed(name=name, url=url, enabled=True))
    set_kv(db, SEEDED_KEY, True)
    db.flush()


def track_keywords(db: DB) -> list[str]:
    saved = get_kv(db, KEYWORDS_KEY)
    return list(saved) if isinstance(saved, list) else list(DEFAULT_TRACK_KEYWORDS)


def add_feed(db: DB, url: str, name: str) -> NewsFeed:
    url, name = url.strip(), name.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("Feed addresses start with https://")
    if db.scalars(select(NewsFeed).where(NewsFeed.url == url)).first():
        raise ValueError("That feed is already in the list")
    feed = NewsFeed(url=url[:500], name=(name or url.split("/")[2])[:100], enabled=True)
    db.add(feed)
    db.flush()
    return feed


# ---------------------------------------------------------------------------- parsing


def _plain(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


def _when(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


@dataclass
class Headline:
    title: str
    link: str
    published_at: datetime | None


def parse_feed(body: bytes) -> list[Headline]:
    """Headlines from an RSS/Atom document: title, link and date only."""
    parsed = feedparser.parse(body)
    out = []
    for entry in parsed.entries:
        title = " ".join(str(entry.get("title") or "").split())
        link = str(entry.get("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        out.append(Headline(title[:300], link[:500], _when(entry)))
    return out


# ---------------------------------------------------------------------------- tagging


@dataclass
class TagContext:
    patterns: list[tuple[str, re.Pattern[str]]]

    def tags(self, title: str) -> list[str]:
        text = _plain(title)
        return sorted({tag for tag, rx in self.patterns if rx.search(text)})


def _word(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(_plain(term)) + r"(?![\w-])")


def _team_names(name: str) -> set[str]:
    short = _TEAM_SUFFIXES.sub("", name).strip()
    return {n for n in (name, short) if len(n) >= 4}


def tag_context(
    db: DB, now: datetime | None = None, keywords: list[str] | None = None
) -> TagContext:
    now = now or utcnow()
    season = (
        db.scalar(
            select(StandingsSnapshot.season).order_by(StandingsSnapshot.season.desc()).limit(1)
        )
        or now.year
    )
    driver_ids = set(
        db.scalars(
            select(StandingsSnapshot.entity_id).where(
                StandingsSnapshot.season == season, StandingsSnapshot.type == "driver"
            )
        )
    )
    team_ids = set(
        db.scalars(
            select(StandingsSnapshot.entity_id).where(
                StandingsSnapshot.season == season, StandingsSnapshot.type == "constructor"
            )
        )
    )
    if not driver_ids:  # no standings yet: use whoever has results this season
        rows = db.execute(
            select(Result.driver_id, Result.constructor_id)
            .join(Session, Result.session_id == Session.id)
            .join(Event, Session.event_id == Event.id)
            .where(Event.season == season)
        ).all()
        driver_ids = {d for d, _ in rows}
        team_ids = {c for _, c in rows if c}
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for d in db.scalars(select(Driver).where(Driver.id.in_(driver_ids))):
        patterns.append((f"driver:{d.id}", _word(d.family_name)))
    for c in db.scalars(select(Constructor).where(Constructor.id.in_(team_ids))):
        for n in _team_names(c.name):
            patterns.append((f"team:{c.id}", _word(n)))
    event = current_or_next_event(db, now)
    if event is not None:
        c = event.circuit
        names = {
            c.name,
            c.locality,
            c.country,
            event.name,
            event.name.replace(" Grand Prix", " GP"),
        }
        for n in names:
            if n and len(n) >= 4:
                patterns.append((f"circuit:{c.id}", _word(n)))
    for kw in keywords if keywords is not None else track_keywords(db):
        if kw.strip():
            patterns.append(("track", _word(kw.strip())))
    return TagContext(patterns)


# ---------------------------------------------------------------------------- fetching


@dataclass
class FeedResult:
    feed_id: int
    new_items: list[int]


def fetch_feed(
    client: httpx.Client, feed: NewsFeed
) -> tuple[list[Headline] | None, str | None, str | None]:
    """(headlines or None if unchanged, etag, last-modified). Raises httpx.HTTPError."""
    headers = {}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.modified:
        headers["If-Modified-Since"] = feed.modified
    resp = client.get(feed.url, headers=headers)
    if resp.status_code == 304:
        return None, feed.etag, feed.modified
    resp.raise_for_status()
    if len(resp.content) > MAX_FEED_BYTES:
        raise httpx.HTTPError(f"feed larger than {MAX_FEED_BYTES} bytes")
    items = parse_feed(resp.content)
    if not items and b"<rss" not in resp.content[:2000] and b"<feed" not in resp.content[:2000]:
        raise httpx.HTTPError("not an RSS or Atom feed")
    return items, resp.headers.get("ETag"), resp.headers.get("Last-Modified")


def make_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, */*",
        },
        timeout=20.0,
        follow_redirects=True,
        transport=transport,
    )


def sync(client: httpx.Client, now: datetime | None = None) -> list[int]:
    """Poll every enabled feed. Returns ids of newly stored items. A failing feed is logged
    and recorded on the feed; the others carry on."""
    now = now or utcnow()
    with session_scope() as db:
        seed_feeds(db)
        feeds = [(f.id, f) for f in db.scalars(select(NewsFeed).where(NewsFeed.enabled))]
        for _, f in feeds:
            db.expunge(f)
    new_ids: list[int] = []
    for feed_id, feed in feeds:
        try:
            items, etag, modified = fetch_feed(client, feed)
        except httpx.HTTPError as exc:
            log.warning("news feed failed", extra={"feed": feed.url, "error": str(exc)})
            with session_scope() as db:
                row = db.get(NewsFeed, feed_id)
                if row:
                    row.last_error_at, row.last_error = now, str(exc)[:500]
            continue
        with session_scope() as db:
            row = db.get(NewsFeed, feed_id)
            if row is None:
                continue
            row.last_success_at, row.last_error = now, None
            row.etag, row.modified = (etag or None), (modified or None)
            if items is None:
                continue
            ctx = tag_context(db, now)
            have = set(
                db.scalars(select(NewsItem.link).where(NewsItem.link.in_([i.link for i in items])))
            )
            for h in items:
                if h.link in have:
                    continue
                have.add(h.link)
                item = NewsItem(
                    feed_id=feed_id,
                    link=h.link,
                    title=h.title,
                    source=row.name,
                    published_at=h.published_at
                    if h.published_at and h.published_at <= now + timedelta(hours=1)
                    else None,
                    fetched_at=now,
                    tags_json=json.dumps(ctx.tags(h.title)),
                )
                db.add(item)
                db.flush()
                new_ids.append(item.id)
    with session_scope() as db:
        db.query(NewsItem).filter(NewsItem.fetched_at < now - timedelta(days=KEEP_DAYS)).delete()
    if new_ids:
        log.info("news headlines stored", extra={"items": len(new_ids)})
    return new_ids


# ---------------------------------------------------------------------------- reading


@dataclass
class NewsView:
    items: list[NewsItem]
    hidden: int
    held_event: Event | None


def tags_of(item: NewsItem) -> list[str]:
    try:
        tags = json.loads(item.tags_json or "[]")
    except ValueError:
        return []
    return [t for t in tags if isinstance(t, str)]


def visible(
    db: DB,
    user_id: int,
    settings: AppSettings,
    *,
    limit: int = 50,
    match: Callable[[list[str]], bool] | None = None,
    reveal: bool = False,
    now: datetime | None = None,
) -> NewsView:
    """Newest headlines first, minus anything spoiler mode is holding back."""
    cut = None if reveal else spoilers.cutoff(db, user_id, settings, now)
    q = select(NewsItem).order_by(NewsItem.fetched_at.desc(), NewsItem.id.desc()).limit(1000)
    items, hidden = [], 0
    for item in db.scalars(q):
        if match is not None and not match(tags_of(item)):
            continue
        if spoilers.hides(cut, item.published_at or item.fetched_at):
            hidden += 1
            continue
        if len(items) < limit:
            items.append(item)
    items.sort(key=lambda i: i.published_at or i.fetched_at, reverse=True)
    return NewsView(items, hidden, cut[0] if cut else None)


def track_updates(
    db: DB, event: Event, user_id: int, settings: AppSettings, limit: int = 10, **kw: Any
) -> NewsView:
    tag = f"circuit:{event.circuit_id}"
    return visible(
        db, user_id, settings, limit=limit, match=lambda t: tag in t and "track" in t, **kw
    )


# ---------------------------------------------------------------------------- alerts


def track_news_message(
    db: DB, item_id: int, user_id: int, now: datetime | None = None
) -> Message | None:
    """An alert for a track-change headline about the upcoming circuit (None otherwise)."""
    now = now or utcnow()
    item = db.get(NewsItem, item_id)
    event = current_or_next_event(db, now)
    if item is None or event is None:
        return None
    tags = tags_of(item)
    if "track" not in tags or f"circuit:{event.circuit_id}" not in tags:
        return None
    when = item.published_at or item.fetched_at
    if now - when > ALERT_MAX_AGE:
        return None
    start = spoilers.first_competitive_start(event)
    return Message(
        user_id=user_id,
        type="track_news",
        title=f"🛠️ Track news for the {event.name}: {item.title}",
        text=f"{item.title}\n{item.source}: {item.link}\n{event_url(event)}",
        event_id=event.id,
        url=item.link,
        data={"headline": item.title, "link": item.link, "source": item.source},
        dedup_content=f"news:{item.link}",
        spoiler=start is not None and when >= start,
    )


def tag_labels(db: DB, items: list[NewsItem]) -> dict[str, str]:
    """Readable labels for the tags on these items ("driver:norris" -> "Norris")."""
    labels: dict[str, str] = {"track": "track change"}
    for item in items:
        for tag in tags_of(item):
            if tag in labels:
                continue
            kind, _, ident = tag.partition(":")
            if kind == "driver":
                d = db.get(Driver, ident)
                labels[tag] = d.family_name if d else ident
            elif kind == "team":
                c = db.get(Constructor, ident)
                labels[tag] = c.name if c else ident
            elif kind == "circuit":
                ev = db.scalars(
                    select(Event).where(Event.circuit_id == ident).order_by(Event.season.desc())
                ).first()
                labels[tag] = ev.circuit.locality or ev.circuit.name if ev else ident
    return labels
