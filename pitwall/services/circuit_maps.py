"""Circuit map images.

Resolution order for a circuit:
1. `map_image` in circuits.yaml: an https:// URL, or a path relative to the data directory;
2. an image you drop into data/maps/<circuitId>.(png|jpg|jpeg|webp|svg);
3. otherwise Pitwall draws one: it takes the fastest lap of the most recent race at that circuit
   (2023+) from OpenF1 car-position data and renders the racing line as a PNG in data/maps/.
Local images are served at /circuit-maps/<circuitId> (no login, so emails can show them)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select

from pitwall.config import get_env
from pitwall.db import session_scope, utcnow
from pitwall.models import Circuit, Event, Result, Session
from pitwall.settings_store import get_kv, set_kv
from pitwall.sources.http import SourceUnavailable
from pitwall.sources.openf1 import FIRST_SEASON, OpenF1Client, match_session

log = logging.getLogger(__name__)

EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg")
CIRCUIT_ID = re.compile(r"^[a-z0-9_]{1,64}$")
ATTEMPTS_KEY = "map_attempts"
RETRY_AFTER = timedelta(days=7)
SIZE = (800, 500)
PAD = 40
LINE = (225, 6, 0, 255)


def maps_dir() -> Path:
    return get_env().data_dir / "maps"


def local_file(circuit: Circuit) -> Path | None:
    """The local image for a circuit, if there is one (never outside the data directory)."""
    data = get_env().data_dir.resolve()
    if circuit.map_image and not circuit.map_image.startswith(("http://", "https://")):
        path = (data / circuit.map_image).resolve()
        if path.is_file() and data in path.parents:
            return path
    if not CIRCUIT_ID.match(circuit.id):
        return None
    for ext in EXTENSIONS:
        path = maps_dir() / f"{circuit.id}{ext}"
        if path.is_file():
            return path
    return None


def map_url(circuit: Circuit) -> str | None:
    if circuit.map_image and circuit.map_image.startswith(("http://", "https://")):
        return circuit.map_image
    if local_file(circuit) is not None:
        return f"{get_env().base_url.rstrip('/')}/circuit-maps/{circuit.id}"
    return None


def render(points: list[tuple[float, float]], path: Path) -> None:
    """Draw the racing line as a transparent PNG, keeping the track's aspect ratio."""
    scale = 2  # supersample for smooth lines
    w, h = SIZE[0] * scale, SIZE[1] * scale
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    span_x, span_y = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    k = min((w - 2 * PAD * scale) / span_x, (h - 2 * PAD * scale) / span_y)
    off_x = (w - span_x * k) / 2
    off_y = (h - span_y * k) / 2
    pts = [(off_x + (x - min(xs)) * k, h - (off_y + (y - min(ys)) * k)) for x, y in points]
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.line([*pts, pts[0]], fill=LINE, width=12 * scale // 2, joint="curve")
    sx, sy = pts[0]
    r = 9 * scale
    draw.ellipse(
        (sx - r, sy - r, sx + r, sy + r),
        fill=(255, 255, 255, 255),
        outline=(17, 17, 17, 255),
        width=3 * scale,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.resize(SIZE, Image.LANCZOS).save(path, optimize=True)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def lap_points(
    laps: list[dict[str, Any]], location: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """x/y positions during the fastest timed lap."""
    timed = [
        lap
        for lap in laps
        if lap.get("lap_duration") and lap.get("date_start") and not lap.get("is_pit_out_lap")
    ]
    if not timed:
        return []
    best = min(timed, key=lambda lap: lap["lap_duration"])
    start = _parse(best["date_start"])
    end = start + timedelta(seconds=float(best["lap_duration"]))
    out = []
    for p in location:
        try:
            when = _parse(p["date"])
        except (KeyError, ValueError):
            continue
        if start <= when <= end and p.get("x") is not None and p.get("y") is not None:
            out.append((float(p["x"]), float(p["y"])))
    return out


def _candidates(now: datetime) -> list[tuple[str, int, datetime, int]]:
    """(circuit id, season, race start, winner's car number) for circuits on this or next
    season's calendar that have no map, using their most recent race since 2023."""
    with session_scope() as db:
        attempts: dict[str, str] = get_kv(db, ATTEMPTS_KEY, {}) or {}
        upcoming = set(
            db.scalars(
                select(Event.circuit_id).where(
                    Event.season >= now.year, Event.status == "scheduled"
                )
            )
        )
        out = []
        for cid in sorted(upcoming):
            circuit = db.get(Circuit, cid)
            if circuit is None or map_url(circuit) is not None:
                continue
            last = attempts.get(cid)
            if last and now - datetime.fromisoformat(last) < RETRY_AFTER:
                continue
            row = db.execute(
                select(Event.season, Session.start_utc, Result.number)
                .join(Session, Session.event_id == Event.id)
                .join(Result, Result.session_id == Session.id)
                .where(
                    Event.circuit_id == cid,
                    Event.season >= FIRST_SEASON,
                    Session.type == "RACE",
                    Session.start_utc < now - timedelta(hours=6),
                    Result.position == 1,
                    Result.number.is_not(None),
                )
                .order_by(Session.start_utc.desc())
                .limit(1)
            ).first()
            if row is not None:
                out.append((cid, row[0], row[1], row[2]))
    return out


def generate_missing(client: OpenF1Client, now: datetime | None = None, limit: int = 5) -> int:
    """Draw maps for up to `limit` circuits. Returns how many were created."""
    now = now or utcnow()
    made = 0
    for cid, season, start, number in _candidates(now)[:limit]:
        try:
            key = match_session(client.sessions(season, "Race"), start)
            points: list[tuple[float, float]] = []
            if key is not None:
                points = lap_points(client.laps(key, number), client.location(key, number))
        except SourceUnavailable:
            log.warning("OpenF1 unavailable; circuit maps will be retried later")
            break
        with session_scope() as db:
            attempts = get_kv(db, ATTEMPTS_KEY, {}) or {}
            attempts[cid] = now.isoformat()
            set_kv(db, ATTEMPTS_KEY, attempts)
        if len(points) < 50:
            log.info("not enough position data for a circuit map", extra={"circuit": cid})
            continue
        render(points, maps_dir() / f"{cid}.png")
        made += 1
        log.info("circuit map drawn", extra={"circuit": cid, "season": season})
    return made
