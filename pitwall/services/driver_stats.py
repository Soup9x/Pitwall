"""Followed-driver tracker: per-race results, averages and the points trend for a season."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import Driver, DriverSessionDetail, Event, Result, Session
from pitwall.services.results_util import classified, dnf
from pitwall.sources.openf1 import fmt_lap


@dataclass
class RaceRow:
    event: Event
    team: str | None
    grid: int | None
    finish: str
    position: int | None
    gained: int | None
    status: str
    points: float  # race + sprint points this weekend
    sprint: str | None
    dnf: bool
    cumulative: float = 0.0
    best_lap: str | None = None
    pit_count: int | None = None
    stints: list[dict] = field(default_factory=list)


@dataclass
class DriverSeason:
    driver: Driver
    season: int
    rows: list[RaceRow]

    @property
    def points(self) -> float:
        return self.rows[-1].cumulative if self.rows else 0.0

    @property
    def avg_grid(self) -> float | None:
        grids = [r.grid for r in self.rows if r.grid]
        return round(sum(grids) / len(grids), 1) if grids else None

    @property
    def avg_finish(self) -> float | None:
        pos = [r.position for r in self.rows if r.position is not None and not r.dnf]
        return round(sum(pos) / len(pos), 1) if pos else None

    @property
    def positions_gained(self) -> int:
        return sum(r.gained for r in self.rows if r.gained is not None)

    @property
    def dnfs(self) -> int:
        return sum(1 for r in self.rows if r.dnf)

    @property
    def has_openf1(self) -> bool:
        return any(r.best_lap or r.stints for r in self.rows)


def seasons_for(db: DB, driver_id: str) -> list[int]:
    return list(
        db.scalars(
            select(Event.season)
            .join(Session)
            .join(Result, Result.session_id == Session.id)
            .where(Result.driver_id == driver_id)
            .distinct()
            .order_by(Event.season.desc())
        )
    )


def season(db: DB, driver_id: str, year: int) -> DriverSeason | None:
    driver = db.get(Driver, driver_id)
    if driver is None:
        return None
    rows = db.execute(
        select(Result, Session, Event)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(
            Result.driver_id == driver_id,
            Event.season == year,
            Session.type.in_(("RACE", "SPRINT")),
        )
        .order_by(Event.round)
    ).all()
    details = {
        d.session_id: d
        for d in db.scalars(
            select(DriverSessionDetail).where(DriverSessionDetail.driver_id == driver_id)
        )
    }
    races: dict[int, RaceRow] = {}
    sprint_pts: dict[int, tuple[float, str]] = {}
    for res, sess, ev in rows:
        if sess.type == "SPRINT":
            sprint_pts[ev.id] = (res.points or 0.0, res.position_text or "")
            continue
        gained = None
        if res.grid and res.position is not None and classified(res):
            gained = res.grid - res.position
        row = RaceRow(
            event=ev,
            team=res.constructor.name if res.constructor else None,
            grid=res.grid or None,
            finish=f"P{res.position_text}" if classified(res) else (res.position_text or "?"),
            position=res.position if classified(res) else None,
            gained=gained,
            status=res.status or "",
            points=res.points or 0.0,
            sprint=None,
            dnf=dnf(res),
        )
        detail = details.get(sess.id)
        if detail is not None:
            row.best_lap = fmt_lap(detail.best_lap_s) if detail.best_lap_s else None
            row.pit_count = detail.pit_count
            row.stints = json.loads(detail.stints_json or "[]")
        races[ev.id] = row
    total = 0.0
    out = []
    for row in sorted(races.values(), key=lambda r: r.event.round):
        if row.event.id in sprint_pts:
            pts, pos = sprint_pts[row.event.id]
            row.points += pts
            row.sprint = f"P{pos}" if pos.isdigit() else pos
        total += row.points
        row.cumulative = total
        out.append(row)
    return DriverSeason(driver, year, out)


def trend_svg(
    series: list[tuple[str, list[float]]], labels: list[str], width: int = 640, height: int = 220
) -> str:
    """Server-rendered SVG line chart of cumulative points (no JavaScript needed)."""
    if not labels or not series:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 12, 12, 28
    top = max((max(v) for _, v in series if v), default=0) or 1
    n = len(labels)

    def x(i: int) -> float:
        return pad_l + (width - pad_l - pad_r) * (i / max(n - 1, 1))

    def y(v: float) -> float:
        return pad_t + (height - pad_t - pad_b) * (1 - v / top)

    colors = ["var(--accent)", "var(--flag-blue)", "var(--flag-yellow)", "var(--timing-purple)"]
    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'aria-label="Cumulative points by round">'
    ]
    for frac in (0, 0.5, 1):
        gy = y(top * frac)
        parts.append(
            f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end">{round(top * frac)}</text>'
        )
    step = max(1, n // 12)
    for i, label in enumerate(labels):
        if i % step == 0 or i == n - 1:
            parts.append(
                f'<text x="{x(i):.1f}" y="{height - 8}" text-anchor="middle">{escape(label)}</text>'
            )
    for k, (name, values) in enumerate(series):
        color = colors[k % len(colors)]
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
        parts.append(
            f'<polyline points="{pts}" fill="none" style="stroke:{color}" '
            'stroke-width="2.5" stroke-linejoin="round"/>'
        )
        for i, v in enumerate(values):
            parts.append(
                f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" style="fill:{color}">'
                f"<title>{escape(name)}: {v:g} pts after {escape(labels[i])}</title></circle>"
            )
    parts.append("</svg>")
    return "".join(parts)
