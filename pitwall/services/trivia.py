"""Trivia engine. Every fact is computed from stored results: nothing is hard-coded or guessed.

Each fact carries a `source` describing the query it came from. Career totals (starts, wins,
points) are only stated when Pitwall has the driver's whole career: their first season must be
later than the earliest season loaded, every season since must be fully backfilled, and they
must have debuted young enough (≤ 25) that an earlier, un-loaded stint is implausible. If any
of that is uncertain, the fact is simply not generated."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from pitwall.models import (
    Constructor,
    Driver,
    Event,
    FollowedDriver,
    Result,
    Season,
    Session,
    StandingsSnapshot,
)
from pitwall.services.results_util import classified, dnf, started
from pitwall.services.standings import fmt_points
from pitwall.settings_store import get_kv, set_kv

MAX_DEBUT_AGE = 25
RECENT_KEY = "trivia_recent"
RECENT_PREVIEWS = 3

# Ergast nationality -> Ergast circuit country names.
NATIONALITY_COUNTRIES: dict[str, set[str]] = {
    "American": {"USA", "United States"},
    "Argentine": {"Argentina"},
    "Argentinian": {"Argentina"},
    "Australian": {"Australia"},
    "Austrian": {"Austria"},
    "Azerbaijani": {"Azerbaijan"},
    "Bahraini": {"Bahrain"},
    "Belgian": {"Belgium"},
    "Brazilian": {"Brazil"},
    "British": {"UK", "United Kingdom", "Great Britain"},
    "Canadian": {"Canada"},
    "Chinese": {"China"},
    "Danish": {"Denmark"},
    "Dutch": {"Netherlands"},
    "Emirati": {"UAE", "United Arab Emirates"},
    "Finnish": {"Finland"},
    "French": {"France"},
    "German": {"Germany"},
    "Hungarian": {"Hungary"},
    "Indian": {"India"},
    "Italian": {"Italy"},
    "Japanese": {"Japan"},
    "Korean": {"Korea", "South Korea"},
    "Malaysian": {"Malaysia"},
    "Mexican": {"Mexico"},
    "Monegasque": {"Monaco"},
    "Portuguese": {"Portugal"},
    "Qatari": {"Qatar"},
    "Russian": {"Russia"},
    "Saudi": {"Saudi Arabia"},
    "Singaporean": {"Singapore"},
    "Spanish": {"Spain"},
    "Swedish": {"Sweden"},
    "Swiss": {"Switzerland"},
    "Thai": {"Thailand"},
    "Turkish": {"Turkey"},
}


@dataclass
class Fact:
    type: str  # milestone, streak, on_this_day, home_race, rookie_veteran
    key: str  # stable id used to avoid repeating facts
    text: str
    score: float
    source: str
    subject: str | None = None  # driver id, for the followed-driver bonus


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _age(dob: date, on: date) -> tuple[int, int]:
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    last_birthday = (
        dob.replace(year=dob.year + years)
        if not (dob.month == 2 and dob.day == 29)
        else date(dob.year + years, 3, 1)
    )
    return years, (on - last_birthday).days


@dataclass
class Context:
    event: Event
    on: date
    data_from: int
    backfilled: set[int]
    drivers: list[Driver]
    constructors: list[Constructor]
    followed: set[str]
    # driver id -> [(event, result)] of races before this event, oldest first
    history: dict[str, list[tuple[Event, Result]]] = field(default_factory=dict)
    career_points: dict[str, float] = field(default_factory=dict)

    def complete(self, driver: Driver) -> bool:
        hist = self.history.get(driver.id)
        if not hist:
            return False
        first = hist[0][0].season
        if first <= self.data_from:
            return False
        if any(y not in self.backfilled for y in range(first, self.event.season)):
            return False
        if driver.dob is None:
            return False
        first_date = hist[0][0].end_date or date(first, 7, 1)
        return _age(driver.dob, first_date)[0] <= MAX_DEBUT_AGE


def build_context(db: DB, event: Event) -> Context | None:
    data_from = db.scalar(
        select(func.min(Event.season)).join(Session).join(Result, Result.session_id == Session.id)
    )
    if data_from is None:
        return None
    season = event.season
    latest = db.scalar(
        select(func.max(StandingsSnapshot.season)).where(StandingsSnapshot.season <= season)
    )
    driver_ids: list[str] = []
    con_ids: list[str] = []
    if latest is not None:
        rnd = db.scalar(
            select(func.max(StandingsSnapshot.round)).where(StandingsSnapshot.season == latest)
        )
        snaps = db.scalars(
            select(StandingsSnapshot)
            .where(StandingsSnapshot.season == latest, StandingsSnapshot.round == rnd)
            .order_by(StandingsSnapshot.position)
        )
        for s in snaps:
            (driver_ids if s.type == "driver" else con_ids).append(s.entity_id)
    drivers = [d for d in (db.get(Driver, i) for i in driver_ids) if d]
    cons = [c for c in (db.get(Constructor, i) for i in con_ids) if c]
    ctx = Context(
        event=event,
        on=event.end_date or event.start_date or date(season, 7, 1),
        data_from=data_from,
        backfilled={s.year for s in db.scalars(select(Season).where(Season.backfilled))},
        drivers=drivers,
        constructors=cons,
        followed={f.driver_id for f in db.scalars(select(FollowedDriver))},
    )
    rows = db.execute(
        select(Result, Event)
        .join(Session, Result.session_id == Session.id)
        .join(Event, Session.event_id == Event.id)
        .where(
            Result.driver_id.in_(driver_ids), Session.type == "RACE", Event.status == "scheduled"
        )
        .order_by(Event.season, Event.round)
    ).all()
    for res, ev in rows:
        if (ev.season, ev.round) < (season, event.round):
            ctx.history.setdefault(res.driver_id, []).append((ev, res))
    pts = db.execute(
        select(Result.driver_id, func.sum(Result.points))
        .join(Session)
        .join(Event)
        .where(
            Result.driver_id.in_(driver_ids),
            Session.type.in_(("RACE", "SPRINT")),
            (Event.season < season) | ((Event.season == season) & (Event.round < event.round)),
        )
        .group_by(Result.driver_id)
    ).all()
    ctx.career_points = {d: float(p or 0) for d, p in pts}
    return ctx


# ---------------------------------------------------------------------------- generators


def milestones(db: DB, ctx: Context) -> list[Fact]:
    facts: list[Fact] = []
    for d in ctx.drivers:
        if not ctx.complete(d):
            continue
        hist = ctx.history[d.id]
        starts = sum(1 for _, r in hist if started(r))
        wins = sum(1 for _, r in hist if r.position == 1 and classified(r))
        podiums = sum(1 for _, r in hist if r.position and r.position <= 3 and classified(r))
        src = (
            f"race results for driver={d.id}, {hist[0][0].season}–{hist[-1][0].season}: "
            f"{starts} starts, {wins} wins, {podiums} podiums"
        )
        nxt = starts + 1
        if nxt >= 50 and nxt % 50 == 0:
            facts.append(
                Fact(
                    "milestone",
                    f"starts:{d.id}:{nxt}",
                    f"{d.full_name} is set to make their {ordinal(nxt)} Grand Prix start.",
                    70 + nxt / 25,
                    src,
                    d.id,
                )
            )
        if wins >= 4 and (wins + 1) % 5 == 0:
            facts.append(
                Fact(
                    "milestone",
                    f"wins:{d.id}:{wins + 1}",
                    f"A win would be {d.full_name}'s {ordinal(wins + 1)} Grand Prix victory.",
                    50 + (wins + 1) / 5,
                    src,
                    d.id,
                )
            )
        recent = hist[-5:]
        if wins == 0 and starts:
            near = [(ev, r) for ev, r in recent if r.position in (2, 3) and classified(r)]
            if near:
                ev, r = min(near, key=lambda x: x[1].position or 99)
                facts.append(
                    Fact(
                        "milestone",
                        f"firstwin:{d.id}:{starts}",
                        f"{d.full_name} is still chasing a first Grand Prix win after {starts} "
                        f"starts — they were P{r.position} at the {ev.season} {ev.short_name}.",
                        55,
                        src,
                        d.id,
                    )
                )
        elif podiums == 0 and starts:
            near = [(ev, r) for ev, r in recent if r.position and r.position <= 6 and classified(r)]
            if near:
                ev, r = min(near, key=lambda x: x[1].position or 99)
                facts.append(
                    Fact(
                        "milestone",
                        f"firstpodium:{d.id}:{starts}",
                        f"{d.full_name} is chasing a first podium after {starts} starts; "
                        f"their best recent result is P{r.position} at the {ev.season} "
                        f"{ev.short_name}.",
                        45,
                        src,
                        d.id,
                    )
                )

    # Most wins at this circuit (within the seasons Pitwall has loaded).
    winners = Counter(
        db.scalars(
            select(Result.driver_id)
            .join(Session)
            .join(Event)
            .where(
                Event.circuit_id == ctx.event.circuit_id,
                Session.type == "RACE",
                Result.position == 1,
                Event.season < ctx.event.season,
            )
        )
    )
    if winners:
        record = max(winners.values())
        holders = [dr for dr, n in winners.items() if n == record]
        circuit = ctx.event.circuit.name
        src = (
            f"race winners at circuit={ctx.event.circuit_id}, seasons "
            f"{ctx.data_from}–{ctx.event.season - 1}"
        )
        active = {d.id: d for d in ctx.drivers}
        if record >= 2 and len(holders) == 1 and holders[0] in active:
            d = active[holders[0]]
            facts.append(
                Fact(
                    "milestone",
                    f"circuitrecord:{d.id}:{ctx.event.circuit_id}:{record}",
                    f"{d.full_name} has won at {circuit} {record} times — more than "
                    f"anyone since {ctx.data_from}.",
                    55,
                    src,
                    d.id,
                )
            )
        for did, n in winners.items():
            if did in active and n == record - 1 and n >= 1 and did not in holders:
                holder = db.get(Driver, holders[0])
                if holder is None:
                    continue
                who = holder.full_name + (" and others" if len(holders) > 1 else "")
                facts.append(
                    Fact(
                        "milestone",
                        f"circuitlevel:{did}:{ctx.event.circuit_id}:{record}",
                        f"A win would put {active[did].full_name} level with {who} on {record} "
                        f"wins at {circuit} (since {ctx.data_from}).",
                        60,
                        src,
                        did,
                    )
                )
    return facts


def streaks(db: DB, ctx: Context) -> list[Fact]:
    facts: list[Fact] = []
    checks = [
        (
            "points",
            5,
            lambda r: (r.points or 0) > 0,
            "has scored points in {n} consecutive races",
            lambda n: min(30 + 2 * n, 70),
        ),
        (
            "podium",
            3,
            lambda r: bool(r.position and r.position <= 3 and classified(r)),
            "has finished on the podium in {n} consecutive races",
            lambda n: min(40 + 4 * n, 80),
        ),
        (
            "frontrow",
            3,
            lambda r: r.grid in (1, 2),
            "has started from the front row in {n} consecutive races",
            lambda n: min(40 + 4 * n, 80),
        ),
        ("dnf", 2, dnf, "has failed to finish the last {n} races", lambda n: 45 + 5 * n),
    ]
    for d in ctx.drivers:
        hist = [r for _, r in ctx.history.get(d.id, []) if started(r)]
        if not hist:
            continue
        for name, minimum, test, template, score in checks:
            n = 0
            for r in reversed(hist):
                if not test(r):
                    break
                n += 1
            if n < minimum:
                continue
            whole = n == len(hist) and not ctx.complete(d)
            text = f"{d.full_name} " + template.format(n=("at least " if whole else "") + str(n))
            last = ctx.history[d.id][-1][0]
            facts.append(
                Fact(
                    "streak",
                    f"streak:{name}:{d.id}:{n}",
                    text + ".",
                    score(n),
                    f"last {n} race results for driver={d.id} up to "
                    f"{last.season} round {last.round}",
                    d.id,
                )
            )
    return facts


def on_this_day(db: DB, ctx: Context) -> list[Fact]:
    facts: list[Fact] = []
    active = {d.id for d in ctx.drivers}
    past = db.scalars(
        select(Event).where(
            Event.circuit_id == ctx.event.circuit_id,
            Event.season < ctx.event.season,
            Event.status == "scheduled",
        )
    )
    for ev in past:
        years = ctx.event.season - ev.season
        if years % 5:
            continue
        race = next((s for s in ev.sessions if s.type == "RACE"), None)
        if race is None:
            continue
        win = db.scalars(
            select(Result).where(Result.session_id == race.id, Result.position == 1)
        ).first()
        if win is None:
            continue
        team = f" ({win.constructor.name})" if win.constructor else ""
        start = "from the pit lane" if win.grid == 0 else (f"from P{win.grid}" if win.grid else "")
        text = (
            f"{years} years ago, {win.driver.full_name}{team} won the {ev.season} "
            f"{ev.name}" + (f" {start}" if start else "") + "."
        )
        score = 40 + (10 if years % 10 == 0 else 0) + (15 if win.driver_id in active else 0)
        facts.append(
            Fact(
                "on_this_day",
                f"onthisday:{ev.slug}",
                text,
                score,
                f"race winner of event={ev.slug}",
                win.driver_id,
            )
        )
    return facts


def home_race(db: DB, ctx: Context) -> list[Fact]:
    facts: list[Fact] = []
    country = ctx.event.circuit.country or ""
    circuit = ctx.event.circuit.name
    for d in ctx.drivers:
        if country not in NATIONALITY_COUNTRIES.get(d.nationality or "", set()):
            continue
        here = [
            (ev, r)
            for ev, r in ctx.history.get(d.id, [])
            if ev.circuit_id == ctx.event.circuit_id and started(r)
        ]
        best = min(
            ((r.position, ev.season) for ev, r in here if r.position is not None and classified(r)),
            default=None,
        )
        src = f"driver nationality={d.nationality}; results at circuit={ctx.event.circuit_id}"
        if not here and ctx.complete(d):
            text = f"This is {d.full_name}'s first home Grand Prix at {circuit}."
            score = 60
        elif best:
            text = (
                f"Home race for {d.full_name} ({d.nationality}): their best finish at "
                f"{circuit} is P{best[0]} ({best[1]})."
            )
            score = 50 + (10 if best[0] == 1 else 0)
        else:
            text = f"Home race for {d.full_name} ({d.nationality})."
            score = 40
        facts.append(Fact("home_race", f"home:{d.id}:{ctx.event.slug}", text, score, src, d.id))
    for c in ctx.constructors:
        if country not in NATIONALITY_COUNTRIES.get(c.nationality or "", set()):
            continue
        wins = (
            db.scalar(
                select(func.count())
                .select_from(Result)
                .join(Session)
                .join(Event)
                .where(
                    Event.circuit_id == ctx.event.circuit_id,
                    Event.season < ctx.event.season,
                    Session.type == "RACE",
                    Result.position == 1,
                    Result.constructor_id == c.id,
                )
            )
            or 0
        )
        text = (
            f"Home race for {c.name}: {wins} win{'s' if wins != 1 else ''} at {circuit} "
            f"since {ctx.data_from}."
            if wins
            else f"Home race for {c.name}."
        )
        facts.append(
            Fact(
                "home_race",
                f"homecon:{c.id}:{ctx.event.slug}",
                text,
                45 if wins else 35,
                f"constructor nationality={c.nationality}; wins at circuit={ctx.event.circuit_id}",
            )
        )
    return facts


def rookie_veteran(db: DB, ctx: Context) -> list[Fact]:
    facts: list[Fact] = []
    aged = [(d, _age(d.dob, ctx.on)) for d in ctx.drivers if d.dob]
    if len(aged) >= 2:
        young = min(aged, key=lambda x: x[1])
        old = max(aged, key=lambda x: x[1])
        for (d, (y, days)), word in ((young, "youngest"), (old, "oldest")):
            facts.append(
                Fact(
                    "rookie_veteran",
                    f"age:{word}:{d.id}:{ctx.event.season}",
                    f"{d.full_name} is the {word} driver on the grid at {y} years and {days} days.",
                    30,
                    f"driver dob={d.dob} vs {ctx.on}",
                    d.id,
                )
            )
    prev_loaded = ctx.event.season - 1 in ctx.backfilled
    best_vet: tuple[int, Driver] | None = None
    for d in ctx.drivers:
        hist = ctx.history.get(d.id, [])
        starts = sum(1 for _, r in hist if started(r))
        if hist and hist[0][0].season == ctx.event.season and prev_loaded and starts:
            best = min((r.position for _, r in hist if r.position and classified(r)), default=None)
            pts = fmt_points(ctx.career_points.get(d.id, 0))
            facts.append(
                Fact(
                    "rookie_veteran",
                    f"rookie:{d.id}:{starts}",
                    f"Rookie {d.full_name}: {starts} starts and {pts} points so far"
                    + (f", best finish P{best}." if best else "."),
                    35,
                    f"race/sprint results for driver={d.id} in {ctx.event.season}",
                    d.id,
                )
            )
        if ctx.complete(d) and (best_vet is None or starts > best_vet[0]):
            best_vet = (starts, d)
    if best_vet and best_vet[0] >= 100:
        starts, d = best_vet
        pts = fmt_points(ctx.career_points.get(d.id, 0))
        facts.append(
            Fact(
                "rookie_veteran",
                f"veteran:{d.id}:{starts}",
                f"{d.full_name} is the most experienced driver on the grid: "
                f"{starts} starts and {pts} career points.",
                35,
                f"race/sprint results for driver={d.id}, all loaded seasons",
                d.id,
            )
        )
    return facts


GENERATORS = (milestones, streaks, on_this_day, home_race, rookie_veteran)


def generate(db: DB, event: Event) -> list[Fact]:
    ctx = build_context(db, event)
    if ctx is None:
        return []
    facts: list[Fact] = []
    for gen in GENERATORS:
        facts.extend(gen(db, ctx))
    for f in facts:
        if f.subject and f.subject in ctx.followed:
            f.score += 15
    return facts


def recent_keys(db: DB) -> set[str]:
    return {k for batch in (get_kv(db, RECENT_KEY, []) or []) for k in batch}


def remember_keys(db: DB, keys: list[str]) -> None:
    """Record the facts used in a preview; the last RECENT_PREVIEWS previews are avoided."""
    batches = (get_kv(db, RECENT_KEY, []) or [])[-(RECENT_PREVIEWS - 1) :]
    set_kv(db, RECENT_KEY, [*batches, keys])


def pick(
    facts: list[Fact], exclude: set[str] | None = None, n: int = 5, per_type: int = 2
) -> list[Fact]:
    """The n highest-scoring facts, at most `per_type` of each type, skipping recent ones."""
    exclude = exclude or set()
    chosen: list[Fact] = []
    used: dict[str, int] = defaultdict(int)
    for f in sorted(facts, key=lambda f: (-f.score, f.key)):
        if f.key in exclude or used[f.type] >= per_type:
            continue
        chosen.append(f)
        used[f.type] += 1
        if len(chosen) >= n:
            break
    return chosen


def for_preview(db: DB, event: Event, *, avoid_recent: bool = True) -> list[Fact]:
    return pick(generate(db, event), recent_keys(db) if avoid_recent else set())
