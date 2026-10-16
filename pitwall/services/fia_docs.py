"""FIA event documents (PDF): stewards' decisions and power unit usage, parsed defensively.

The parsers only accept documents that match what they expect. Anything unexpected (a driver
that can't be identified, a usage count that went down, a number out of range) means the whole
document is recorded as `failed` with the reason, nothing from it is stored, and the UI shows a
"couldn't parse - view source" link. An admin can then enter the values by hand."""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import (
    Driver,
    Event,
    FiaDocument,
    GridPenalty,
    LicencePoints,
    PuUsage,
    Result,
    Session,
)
from pitwall.services import penalties

log = logging.getLogger(__name__)


class ParseError(ValueError):
    pass


def extract_text(pdf: bytes) -> str:
    """Text of a PDF (all pages). Raises ParseError if it can't be read."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            text = "\n".join(page.extract_text() or "" for page in doc.pages)
    except Exception as exc:  # pdfplumber raises many types for broken files
        raise ParseError(f"unreadable PDF ({type(exc).__name__})") from exc
    if not text.strip():
        raise ParseError("the PDF has no text layer")
    return text


def classify(title: str) -> str:
    t = title.lower()
    if "power unit" in t and ("used" in t or "usage" in t or "elements" in t):
        return "pu_usage"
    if "decision" in t or "offence" in t or "penalty" in t:
        return "decision"
    return "other"


# ---------------------------------------------------------------------------- decisions


@dataclass
class Decision:
    car: int
    driver_name: str
    reason: str
    grid_places: int | None = None
    back_of_grid: bool = False
    next_event: bool = False  # "for the next race in which the driver participates"
    points: int | None = None
    points_total: int | None = None


_WS = re.compile(r"[ \t]+")
_CAR = re.compile(r"No\s*/\s*Driver\s*:?\s*(\d{1,2})\s*[-–]\s*([^\n]+)", re.I)
_DECISION = re.compile(r"\bDecision\s*:?\s*(.+?)(?:\n\s*Reason\b|\Z)", re.I | re.S)
_FACT = re.compile(r"\bFact\s*:?\s*([^\n]+)", re.I)
_GRID = [
    re.compile(r"(\d{1,2})[\s-]*(?:place|position)s?\s+grid\s+penalty", re.I),
    re.compile(r"drop\s+of\s+(\d{1,2})\s+grid\s+positions?", re.I),
    re.compile(r"(\d{1,2})\s+grid\s+(?:place|position)s?\s+penalty", re.I),
]
_BACK = re.compile(r"back\s+of\s+the\s+(?:starting\s+)?grid", re.I)
_NEXT = re.compile(r"\bnext\s+(?:race|event|competition)\b", re.I)
_POINTS = re.compile(r"\b(\d{1,2})\s+penalty\s+points?\b", re.I)
_TOTAL = re.compile(r"total\s+(?:of\s+)?(\d{1,2})\s+points?", re.I)


def parse_decision(text: str) -> Decision | None:
    """A stewards' decision, or None if it imposes nothing Pitwall tracks (e.g. a reprimand
    or "no further action"). Raises ParseError if the document doesn't look as expected."""
    text = _WS.sub(" ", text)
    car = _CAR.search(text)
    decision = _DECISION.search(text)
    if car is None or decision is None:
        raise ParseError("no 'No / Driver' or 'Decision' section")
    body = decision.group(1).strip()
    fact = _FACT.search(text)
    d = Decision(
        car=int(car.group(1)),
        driver_name=car.group(2).strip(),
        reason=(fact.group(1).strip() if fact else body.splitlines()[0])[:300],
    )
    grids = {int(m.group(1)) for rx in _GRID for m in rx.finditer(body)}
    if len(grids) > 1:
        raise ParseError(f"several grid penalties in one decision: {sorted(grids)}")
    if grids:
        d.grid_places = grids.pop()
        if not 1 <= d.grid_places <= 60:
            raise ParseError(f"implausible grid penalty: {d.grid_places} places")
    d.back_of_grid = bool(_BACK.search(body))
    d.next_event = bool(_NEXT.search(body)) and (d.grid_places is not None or d.back_of_grid)
    pts = {int(m.group(1)) for m in _POINTS.finditer(body)}
    if len(pts) > 1:
        raise ParseError(f"several penalty point figures: {sorted(pts)}")
    if pts:
        d.points = pts.pop()
        total = _TOTAL.search(body)
        d.points_total = int(total.group(1)) if total else None
        if not 1 <= d.points <= penalties.BAN_POINTS or (
            d.points_total is not None and not d.points <= d.points_total <= 24
        ):
            raise ParseError(f"implausible penalty points: {d.points} (total {d.points_total})")
    if d.grid_places is None and not d.back_of_grid and d.points is None:
        return None
    return d


# ---------------------------------------------------------------------------- PU usage

_ELEMENT_TOKENS = {
    "ICE": "ICE",
    "TC": "TC",
    "MGU-H": "MGU-H",
    "MGUH": "MGU-H",
    "MGU-K": "MGU-K",
    "MGUK": "MGU-K",
    "ES": "ES",
    "CE": "CE",
    "EX": "EX",
}


def parse_pu_usage(text: str) -> dict[int, dict[str, int]]:
    """{car number: {element: cumulative count}} from a "power unit elements used" document.

    Needs a header line naming at least three elements, then one line per car:
    `<car> <driver name> <count> <count> ...` with exactly one count per header element."""
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    header: list[str] | None = None
    out: dict[int, dict[str, int]] = {}
    for ln in lines:
        tokens = ln.upper().split()
        elements = [_ELEMENT_TOKENS[t] for t in tokens if t in _ELEMENT_TOKENS]
        if len(elements) >= 3 and len(elements) >= len(tokens) - 3:
            if header is not None and out:
                raise ParseError("more than one usage table; can't tell which is current")
            header = elements
            continue
        if header is None:
            continue
        m = re.match(r"^(\d{1,2})\s+([^\d]+?)\s+((?:\d{1,2}\s+)*\d{1,2})$", ln)
        if not m:
            continue
        counts = [int(x) for x in m.group(3).split()]
        if len(counts) != len(header):
            raise ParseError(f"car {m.group(1)}: {len(counts)} counts for {len(header)} elements")
        car = int(m.group(1))
        if car in out:
            raise ParseError(f"car {car} listed twice")
        out[car] = dict(zip(header, counts, strict=True))
    if header is None or not out:
        raise ParseError("no power unit usage table found")
    return out


# ---------------------------------------------------------------------------- storing


def car_to_driver(db: DB, season: int, car: int) -> Driver | None:
    """The driver who raced number `car` most recently in `season` (or whose permanent number
    it is). None if that's unknown or ambiguous: a wrong driver is worse than no driver."""
    ids = set(
        db.scalars(
            select(Result.driver_id)
            .join(Session, Result.session_id == Session.id)
            .join(Event, Session.event_id == Event.id)
            .where(Event.season == season, Result.number == car)
        )
    )
    if len(ids) == 1:
        return db.get(Driver, ids.pop())
    if not ids:
        owners = list(db.scalars(select(Driver).where(Driver.number == car)))
        return owners[0] if len(owners) == 1 else None
    return None


def next_event(db: DB, event: Event) -> Event | None:
    return db.scalars(
        select(Event)
        .where(
            Event.status == "scheduled",
            (Event.season > event.season)
            | ((Event.season == event.season) & (Event.round > event.round)),
        )
        .order_by(Event.season, Event.round)
        .limit(1)
    ).first()


@dataclass
class IngestResult:
    document: FiaDocument
    grid: list[GridPenalty] = field(default_factory=list)
    points: list[LicencePoints] = field(default_factory=list)
    usage: list[PuUsage] = field(default_factory=list)


def _doc(
    db: DB, url: str, title: str, event: Event, published_at: datetime | None, kind: str
) -> FiaDocument:
    doc = db.scalars(select(FiaDocument).where(FiaDocument.url == url)).first()
    if doc is None:
        doc = FiaDocument(
            url=url, title=title[:300], event_id=event.id, kind=kind, status="pending"
        )
        db.add(doc)
    doc.published_at, doc.fetched_at = published_at, utcnow()
    return doc


def _fail(doc: FiaDocument, why: str) -> None:
    doc.status, doc.detail = "failed", why[:1000]
    log.warning("FIA document not parsed", extra={"title": doc.title, "reason": why})


def ingest(
    db: DB,
    event: Event,
    *,
    url: str,
    title: str,
    pdf: bytes,
    published_at: datetime | None = None,
) -> IngestResult:
    """Parse one document and store what it says. Never raises for bad content."""
    kind = classify(title)
    doc = _doc(db, url, title, event, published_at, kind)
    db.flush()
    result = IngestResult(doc)
    if kind == "other":
        doc.status, doc.detail = "ignored", None
        db.flush()
        return result
    savepoint = db.begin_nested()
    try:
        text = extract_text(pdf)
        if kind == "decision":
            _store_decision(db, event, doc, text, result)
        else:
            _store_usage(db, event, doc, text, result)
        savepoint.commit()
        doc.status, doc.detail = "parsed", None
    except ParseError as exc:
        savepoint.rollback()  # nothing from a document that failed any check is kept
        result.grid, result.points, result.usage = [], [], []
        _fail(doc, str(exc))
    db.flush()
    return result


def _plain(name: str) -> str:
    """Lower case without accents, so 'PÉREZ' matches 'Perez'."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    ).lower()


def _store_decision(
    db: DB, event: Event, doc: FiaDocument, text: str, result: IngestResult
) -> None:
    d = parse_decision(text)
    if d is None:
        return
    driver = car_to_driver(db, event.season, d.car)
    if driver is None:
        raise ParseError(f"can't tell which driver car {d.car} is ({d.driver_name})")
    if d.driver_name and _plain(driver.family_name) not in _plain(d.driver_name):
        raise ParseError(
            f"car {d.car} is {driver.full_name}, but the document names {d.driver_name}"
        )
    db.query(GridPenalty).filter(GridPenalty.document_id == doc.id).delete()
    db.query(LicencePoints).filter(LicencePoints.document_id == doc.id).delete()
    if d.grid_places is not None or d.back_of_grid:
        target = next_event(db, event) if d.next_event else event
        if target is None:
            raise ParseError("the penalty is for the next race, which isn't on the calendar yet")
        row = GridPenalty(
            event_id=target.id,
            driver_id=driver.id,
            places=d.grid_places,
            back_of_grid=d.back_of_grid,
            reason=d.reason,
            origin="fia",
            document=doc,
        )
        db.add(row)
        result.grid.append(row)
    if d.points:
        awarded = (doc.published_at or utcnow()).date()
        row = LicencePoints(
            driver_id=driver.id,
            event_id=event.id,
            points=d.points,
            awarded_on=awarded,
            reason=d.reason,
            origin="fia",
            document=doc,
        )
        db.add(row)
        result.points.append(row)


def _store_usage(db: DB, event: Event, doc: FiaDocument, text: str, result: IngestResult) -> None:
    table = parse_pu_usage(text)
    counts: dict[tuple[str, str], int] = {}
    for car, elements in table.items():
        driver = car_to_driver(db, event.season, car)
        if driver is None:
            raise ParseError(f"can't tell which driver car {car} is")
        for element, n in elements.items():
            counts[(driver.id, element)] = n
    problems = penalties.check_usage(db, event, counts)
    if problems:
        raise ParseError("; ".join(problems[:5]))
    db.query(PuUsage).filter(PuUsage.event_id == event.id, PuUsage.origin == "fia").delete()
    for (driver_id, element), n in counts.items():
        row = PuUsage(
            event_id=event.id,
            driver_id=driver_id,
            element=element,
            count=n,
            origin="fia",
            document=doc,
        )
        db.add(row)
        result.usage.append(row)
