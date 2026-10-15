"""Penalty watch: power unit usage vs allocation, grid penalties and super licence points.

Values come from two places: an admin's manual entries and parsed FIA event documents
(pitwall.services.fia_docs). For the same key a manual entry always wins:

* PU usage:      (event, driver, element)
* grid penalty:  (event, driver)        - a manual "no penalty" row overrules a wrong parse
* licence points (driver, event)        - manual 0 points overrules a wrong parse

Every parsed record keeps a link to its FIA document. Nothing is estimated: unknown values
are left out, and an allocation is only known once an admin enters it."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import (
    Driver,
    Event,
    FiaDocument,
    GridPenalty,
    LicencePoints,
    PuAllocation,
    PuUsage,
)
from pitwall.services import spoilers

log = logging.getLogger(__name__)

# Order used for display. Any other element name an admin or document uses is shown after.
PU_ELEMENTS = ("ICE", "TC", "MGU-H", "MGU-K", "ES", "CE", "EX")
BAN_POINTS = 12
WARN_POINTS = (10, 11)


def norm_element(name: str) -> str:
    e = name.strip().upper().replace(" ", "").replace("_", "-")
    return {"MGUH": "MGU-H", "MGUK": "MGU-K"}.get(e, e)


def expires_on(awarded: date) -> date:
    """Points count for 12 months: they drop off on the same date a year later."""
    try:
        return awarded.replace(year=awarded.year + 1)
    except ValueError:  # 29 February
        return date(awarded.year + 1, 3, 1)


def _event_order(ev: Event) -> tuple[int, int]:
    return (ev.season, ev.round)


# ---------------------------------------------------------------------------- PU usage


@dataclass
class PuCell:
    count: int
    allowed: int | None
    origin: str
    event: Event
    document: FiaDocument | None

    @property
    def state(self) -> str:
        """ok / limit / over (unknown allocation counts as ok, shown without a limit)."""
        if self.allowed is None:
            return "ok"
        if self.count > self.allowed:
            return "over"
        return "limit" if self.count == self.allowed else "ok"


@dataclass
class PuRow:
    driver: Driver
    cells: dict[str, PuCell] = field(default_factory=dict)


def allocations(db: DB, season: int) -> dict[str, int]:
    return {
        a.element: a.allowed
        for a in db.scalars(select(PuAllocation).where(PuAllocation.season == season))
    }


def _effective_usage(db: DB, season: int) -> dict[tuple[int, str, str], PuUsage]:
    """(event_id, driver_id, element) -> the winning row (manual over FIA)."""
    rows = db.scalars(
        select(PuUsage).join(Event, PuUsage.event_id == Event.id).where(Event.season == season)
    )
    best: dict[tuple[int, str, str], PuUsage] = {}
    for r in rows:
        k = (r.event_id, r.driver_id, r.element)
        if k not in best or (r.origin == "manual" and best[k].origin != "manual"):
            best[k] = r
    return best


def pu_table(
    db: DB, season: int, cut: tuple[Event, datetime] | None = None
) -> tuple[list[str], list[PuRow]]:
    """Each driver's latest known count per element this season."""
    alloc = allocations(db, season)
    latest: dict[tuple[str, str], tuple[tuple[int, int], PuUsage]] = {}
    for r in _effective_usage(db, season).values():
        ev = db.get(Event, r.event_id)
        if ev is None or _hidden(r, ev, cut):
            continue
        key = (r.driver_id, r.element)
        if key not in latest or _event_order(ev) > latest[key][0]:
            latest[key] = (_event_order(ev), r)
    rows: dict[str, PuRow] = {}
    seen: set[str] = set()
    for (driver_id, element), (_order, r) in latest.items():
        row = rows.setdefault(driver_id, PuRow(r.driver))
        row.cells[element] = PuCell(
            r.count, alloc.get(element), r.origin, db.get(Event, r.event_id), r.document
        )
        seen.add(element)
    elements = [e for e in PU_ELEMENTS if e in seen or e in alloc]
    elements += sorted((seen | set(alloc)) - set(elements))
    return elements, sorted(rows.values(), key=lambda r: r.driver.family_name)


def previous_counts(db: DB, event: Event) -> dict[tuple[str, str], int]:
    """Latest effective (driver, element) counts from events before `event` this season."""
    out: dict[tuple[str, str], tuple[tuple[int, int], int]] = {}
    for r in _effective_usage(db, event.season).values():
        ev = db.get(Event, r.event_id)
        if ev is None or _event_order(ev) >= _event_order(event):
            continue
        key = (r.driver_id, r.element)
        if key not in out or _event_order(ev) > out[key][0]:
            out[key] = (_event_order(ev), r.count)
    return {k: v[1] for k, v in out.items()}


def check_usage(db: DB, event: Event, counts: dict[tuple[str, str], int]) -> list[str]:
    """Problems that make a set of counts untrustworthy (empty list = looks right)."""
    problems = []
    before = previous_counts(db, event)
    for (driver_id, element), n in counts.items():
        if not 0 < n <= 30:
            problems.append(f"{driver_id} {element}: implausible count {n}")
        prev = before.get((driver_id, element))
        if prev is not None and n < prev:
            problems.append(f"{driver_id} {element}: went down from {prev} to {n}")
    return problems


def set_manual_usage(
    db: DB, event: Event, driver_id: str, element: str, count: int, user_id: int | None
) -> PuUsage:
    element = norm_element(element)
    if not element:
        raise ValueError("Enter a power unit element, e.g. ICE")
    if not 0 < count <= 30:
        raise ValueError("The count must be between 1 and 30")
    if db.get(Driver, driver_id) is None:
        raise ValueError("Unknown driver")
    row = db.scalars(
        select(PuUsage).where(
            PuUsage.event_id == event.id,
            PuUsage.driver_id == driver_id,
            PuUsage.element == element,
            PuUsage.origin == "manual",
        )
    ).first()
    if row is None:
        row = PuUsage(event_id=event.id, driver_id=driver_id, element=element, origin="manual")
        db.add(row)
    row.count, row.recorded_by, row.recorded_at = count, user_id, utcnow()
    db.flush()
    return row


def set_allocation(db: DB, season: int, element: str, allowed: int | None) -> None:
    element = norm_element(element)
    if not element:
        raise ValueError("Enter a power unit element, e.g. ICE")
    row = db.get(PuAllocation, (season, element))
    if allowed is None:
        if row is not None:
            db.delete(row)
    else:
        if not 0 < allowed <= 30:
            raise ValueError("The allocation must be between 1 and 30")
        if row is None:
            db.add(PuAllocation(season=season, element=element, allowed=allowed))
        else:
            row.allowed = allowed
    db.flush()


# ---------------------------------------------------------------------------- grid penalties


def _hidden(
    row: PuUsage | GridPenalty | LicencePoints,
    event: Event | None,
    cut: tuple[Event, datetime] | None,
) -> bool:
    """Spoiler mode: hide what was published (or entered) during/after the held event's
    competitive sessions."""
    if cut is None or event is None or event.id != cut[0].id:
        return False
    published = row.document.published_at if row.document else row.recorded_at
    return spoilers.hides(cut, published, event.id)


def grid_penalties(
    db: DB, event: Event, cut: tuple[Event, datetime] | None = None
) -> list[GridPenalty]:
    rows = list(db.scalars(select(GridPenalty).where(GridPenalty.event_id == event.id)))
    manual_keys = {r.driver_id for r in rows if r.origin == "manual"}
    out = [
        r
        for r in rows
        if (r.origin == "manual" or r.driver_id not in manual_keys)
        and (r.places or r.back_of_grid)
        and not _hidden(r, event, cut)
    ]
    return sorted(out, key=lambda r: (not r.back_of_grid, -(r.places or 0), r.driver.family_name))


def add_manual_grid_penalty(
    db: DB,
    event: Event,
    driver_id: str,
    places: int | None,
    back_of_grid: bool,
    reason: str,
    user_id: int | None,
) -> GridPenalty:
    if db.get(Driver, driver_id) is None:
        raise ValueError("Unknown driver")
    if places is not None and not 0 <= places <= 60:
        raise ValueError("Grid places must be between 0 and 60")
    row = GridPenalty(
        event_id=event.id,
        driver_id=driver_id,
        places=places or None,
        back_of_grid=back_of_grid,
        reason=reason.strip()[:300],
        origin="manual",
        recorded_by=user_id,
        recorded_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def describe_grid(p: GridPenalty) -> str:
    if p.back_of_grid:
        return "back of the grid"
    return f"{p.places}-place grid penalty" if p.places else "no penalty"


# ---------------------------------------------------------------------------- licence points


@dataclass
class PointsEntry:
    row: LicencePoints
    expires: date


@dataclass
class DriverPoints:
    driver: Driver
    total: int
    entries: list[PointsEntry]

    @property
    def state(self) -> str:
        if self.total >= BAN_POINTS:
            return "ban"
        return "danger" if self.total >= WARN_POINTS[0] else ("warn" if self.total >= 8 else "ok")

    @property
    def next_expiry(self) -> PointsEntry | None:
        return min(self.entries, key=lambda e: e.expires) if self.entries else None


def _effective_points(db: DB) -> list[LicencePoints]:
    rows = list(db.scalars(select(LicencePoints)))
    manual = {(r.driver_id, r.event_id) for r in rows if r.origin == "manual" and r.event_id}
    return [
        r
        for r in rows
        if r.origin == "manual" or r.event_id is None or (r.driver_id, r.event_id) not in manual
    ]


def licence_table(
    db: DB, today: date | None = None, cut: tuple[Event, datetime] | None = None
) -> list[DriverPoints]:
    """Live 12-month totals, highest first. Drivers with no live points are left out."""
    today = today or utcnow().date()
    per: dict[str, list[PointsEntry]] = defaultdict(list)
    drivers: dict[str, Driver] = {}
    for r in _effective_points(db):
        ev = db.get(Event, r.event_id) if r.event_id else None
        if r.points <= 0 or _hidden(r, ev, cut):
            continue
        exp = expires_on(r.awarded_on)
        if r.awarded_on <= today < exp:
            per[r.driver_id].append(PointsEntry(r, exp))
            drivers[r.driver_id] = r.driver
    out = [
        DriverPoints(
            drivers[d], sum(e.row.points for e in entries), sorted(entries, key=lambda e: e.expires)
        )
        for d, entries in per.items()
    ]
    return sorted(out, key=lambda p: (-p.total, p.driver.family_name))


def driver_total(db: DB, driver_id: str, today: date | None = None) -> int:
    return next((p.total for p in licence_table(db, today) if p.driver.id == driver_id), 0)


def add_manual_points(
    db: DB,
    driver_id: str,
    points: int,
    awarded_on: date,
    reason: str,
    event: Event | None,
    user_id: int | None,
) -> LicencePoints:
    if db.get(Driver, driver_id) is None:
        raise ValueError("Unknown driver")
    if not 0 <= points <= BAN_POINTS:
        raise ValueError("Points must be between 0 and 12")
    if awarded_on > utcnow().date():
        raise ValueError("The date can't be in the future")
    row = LicencePoints(
        driver_id=driver_id,
        event_id=event.id if event else None,
        points=points,
        awarded_on=awarded_on,
        reason=reason.strip()[:300],
        origin="manual",
        recorded_by=user_id,
        recorded_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------- manual list


def manual_entries(db: DB) -> dict[str, list]:
    return {
        "usage": list(
            db.scalars(
                select(PuUsage)
                .where(PuUsage.origin == "manual")
                .order_by(PuUsage.recorded_at.desc())
            )
        ),
        "grid": list(
            db.scalars(
                select(GridPenalty)
                .where(GridPenalty.origin == "manual")
                .order_by(GridPenalty.recorded_at.desc())
            )
        ),
        "points": list(
            db.scalars(
                select(LicencePoints)
                .where(LicencePoints.origin == "manual")
                .order_by(LicencePoints.recorded_at.desc())
            )
        ),
    }


def failed_documents(db: DB, limit: int = 20) -> list[FiaDocument]:
    return list(
        db.scalars(
            select(FiaDocument)
            .where(FiaDocument.status == "failed")
            .order_by(FiaDocument.fetched_at.desc())
            .limit(limit)
        )
    )
