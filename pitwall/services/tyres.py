"""Pirelli tyre nominations: which compounds (C1 hardest ... C6 softest) are the hard, medium
and soft at an event.

Entered by an admin in the event page form, or parsed from Pirelli's race preview press
release. A manual entry always wins over a parsed one. Nothing is ever guessed: an event with
no nomination shows "Not announced yet" (plus last year's choice at the same circuit)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import COMPOUNDS, Event, TyreNomination

ROLES = ("hard", "medium", "soft")


class InvalidNomination(ValueError):
    pass


def validate(hard: str, medium: str, soft: str) -> tuple[str, str, str]:
    """Normalise ("c2" -> "C2") and check the three are real compounds, hard to soft."""
    picked = tuple(str(c).strip().upper() for c in (hard, medium, soft))
    if any(c not in COMPOUNDS for c in picked):
        raise InvalidNomination("Pick a compound (C1–C6) for hard, medium and soft.")
    idx = [COMPOUNDS.index(c) for c in picked]
    if not idx[0] < idx[1] < idx[2]:
        raise InvalidNomination(
            "The hard must be a harder compound than the medium, and the medium harder than "
            "the soft (C1 is the hardest)."
        )
    return picked  # type: ignore[return-value]


def effective(db: DB, event_id: int) -> TyreNomination | None:
    rows = {
        r.origin: r
        for r in db.scalars(select(TyreNomination).where(TyreNomination.event_id == event_id))
    }
    return rows.get("manual") or rows.get("pirelli")


@dataclass
class TyreView:
    event: Event
    nomination: TyreNomination | None
    last_year: tuple[Event, TyreNomination] | None


def last_year(db: DB, event: Event) -> tuple[Event, TyreNomination] | None:
    """The most recent earlier event at the same circuit that has a nomination."""
    earlier = db.scalars(
        select(Event)
        .where(Event.circuit_id == event.circuit_id, Event.season < event.season)
        .order_by(Event.season.desc(), Event.round.desc())
    )
    for ev in earlier:
        nom = effective(db, ev.id)
        if nom is not None:
            return ev, nom
    return None


def view(db: DB, event: Event) -> TyreView:
    return TyreView(event, effective(db, event.id), last_year(db, event))


def set_manual(
    db: DB,
    event_id: int,
    hard: str,
    medium: str,
    soft: str,
    user_id: int | None,
    source_url: str | None = None,
) -> TyreNomination:
    h, m, s = validate(hard, medium, soft)
    source_url = (source_url or "").strip() or None
    if source_url and not source_url.startswith(("https://", "http://")):
        raise InvalidNomination("The source link must start with https://")
    row = db.scalars(
        select(TyreNomination).where(
            TyreNomination.event_id == event_id, TyreNomination.origin == "manual"
        )
    ).first()
    if row is None:
        row = TyreNomination(event_id=event_id, origin="manual")
        db.add(row)
    row.hard, row.medium, row.soft = h, m, s
    row.source_url, row.source_title = source_url, None
    row.recorded_at, row.recorded_by = utcnow(), user_id
    db.flush()
    return row


def clear_manual(db: DB, event_id: int) -> None:
    db.query(TyreNomination).filter(
        TyreNomination.event_id == event_id, TyreNomination.origin == "manual"
    ).delete()
    db.flush()


def store_parsed(
    db: DB,
    event_id: int,
    picked: tuple[str, str, str],
    *,
    title: str,
    url: str,
    published_at: datetime | None,
) -> TyreNomination:
    h, m, s = validate(*picked)
    row = db.scalars(
        select(TyreNomination).where(
            TyreNomination.event_id == event_id, TyreNomination.origin == "pirelli"
        )
    ).first()
    if row is None:
        row = TyreNomination(event_id=event_id, origin="pirelli")
        db.add(row)
    row.hard, row.medium, row.soft = h, m, s
    row.source_title, row.source_url, row.source_published_at = title[:300], url, published_at
    row.recorded_at = utcnow()
    db.flush()
    return row


# ---------------------------------------------------------------------------- press releases

# "C3 ... hard" or "hard ... C3" within a short distance, without crossing another compound.
_COMPOUND = r"\bC([1-6])\b"
_ROLE = r"\b(hard|medium|soft)\b"
_PAIR_RE = re.compile(
    rf"{_COMPOUND}[^.;:\n]{{0,40}}?{_ROLE}|{_ROLE}[^.;:\n]{{0,25}}?{_COMPOUND}", re.IGNORECASE
)


def parse_press_release(text: str) -> tuple[str, str, str] | None:
    """Find the hard/medium/soft compounds in a race preview's text.

    Returns None unless exactly one compound is found for each role and the result passes
    `validate` (e.g. a release that also mentions last year's choice is ambiguous: no answer
    is better than a wrong one)."""
    found: dict[str, set[str]] = {r: set() for r in ROLES}
    for m in _PAIR_RE.finditer(text):
        if m.group(1):
            compound, role = m.group(1), m.group(2)
        else:
            role, compound = m.group(3), m.group(4)
        found[role.lower()].add(f"C{compound}")
    if any(len(v) != 1 for v in found.values()):
        return None
    picked = tuple(next(iter(found[r])) for r in ROLES)
    try:
        return validate(*picked)
    except InvalidNomination:
        return None
