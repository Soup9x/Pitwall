"""Title math: who is mathematically eliminated, and what the leader needs to clinch next time.

All conditions are *sufficient* (safe) conditions:
- a final tie on points is only counted as the leader's if the rival can't match the leader's
  race wins even by winning every remaining race (the first countback tiebreaker);
- the leader's own fastest-lap point is never assumed, while a rival's is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import Constructor, Driver, Event, StandingsSnapshot
from pitwall.services.points import PointsSystem, system_for
from pitwall.services.standings import fmt_points


@dataclass
class Contender:
    entity_id: str
    name: str
    points: float
    wins: int
    max_points: float = 0.0
    eliminated: bool = False


@dataclass
class TitleMath:
    kind: str  # driver / constructor
    season: int
    remaining_races: int
    remaining_sprints: int
    max_remaining: float
    leader: Contender
    alive: list[Contender]
    eliminated: list[Contender]
    champion: Contender | None = None
    next_event: str | None = None
    next_is_sprint: bool = False
    can_clinch_next: bool = False
    clinch_lines: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        """Worth including in a preview: decided, decidable next time, or a late-season fight."""
        return bool(
            self.champion
            or self.can_clinch_next
            or (self.remaining_races <= 8 and len(self.alive) > 1)
        )

    def summary(self) -> list[str]:
        who = "Drivers'" if self.kind == "driver" else "Constructors'"
        if self.champion:
            return [f"{self.champion.name} has clinched the {self.season} {who} title."]
        lines = []
        if self.alive and len(self.alive) <= 6:
            names = ", ".join(f"{c.name} ({fmt_points(c.points)})" for c in self.alive)
            lines.append(f"{len(self.alive)} still in contention for the {who} title: {names}.")
        else:
            lines.append(f"{len(self.alive)} still in contention for the {who} title.")
        lines.append(
            f"{fmt_points(self.max_remaining)} points left: {self.remaining_races} "
            f"race{'s' if self.remaining_races != 1 else ''}"
            + (
                f" and {self.remaining_sprints} sprint{'s' if self.remaining_sprints != 1 else ''}"
                if self.remaining_sprints
                else ""
            )
            + "."
        )
        if self.can_clinch_next and self.next_event:
            lines.append(f"{self.leader.name} can clinch it at the {self.next_event}:")
            lines.extend(f"  • {line}" for line in self.clinch_lines)
        return lines


def _tie_goes_to_leader(leader: Contender, rival: Contender, races_left: int) -> bool:
    return rival.wins + races_left < leader.wins


def _ordinal_cutoff(rival: str, position: int, leader_pos: int | None) -> str:
    """'X finishes below P{position}' in friendlier words."""
    if position == 1:
        return f"{rival} doesn't win"
    if leader_pos is not None and position == leader_pos:
        return f"{rival} finishes behind them"
    return f"{rival} finishes below P{position}"


def compute(
    kind: str,
    season: int,
    standings: list[Contender],
    system: PointsSystem,
    remaining_races: int,
    remaining_sprints: int,
    next_event: str | None = None,
    next_is_sprint: bool = False,
) -> TitleMath | None:
    """`standings` in championship order; remaining counts *include* the next event."""
    if not standings:
        return None
    team = kind == "constructor"
    race_max = system.team_race_max if team else system.race_max
    sprint_max = system.team_sprint_max if team else system.sprint_max
    max_remaining = remaining_races * race_max + remaining_sprints * sprint_max
    leader = max(standings, key=lambda c: c.points)  # first listed wins ties (countback order)

    for c in standings:
        c.max_points = c.points + max_remaining
        c.eliminated = c is not leader and (
            c.max_points < leader.points
            or (c.max_points == leader.points and _tie_goes_to_leader(leader, c, remaining_races))
        )
    rivals = [c for c in standings if c is not leader and not c.eliminated]
    tm = TitleMath(
        kind,
        season,
        remaining_races,
        remaining_sprints,
        max_remaining,
        leader,
        alive=[leader, *rivals],
        eliminated=[c for c in standings if c.eliminated],
        next_event=next_event,
        next_is_sprint=next_is_sprint,
    )
    if not rivals:
        tm.champion = leader
        return tm
    if remaining_races == 0 or next_event is None:
        return tm

    next_max = race_max + (sprint_max if next_is_sprint else 0)
    after = max_remaining - next_max
    races_after = remaining_races  # wins still available to a rival, counting this event
    # Leader clinches iff (leader's points this event) - (rival's) >= need[rival] for all rivals.
    need = {
        r.entity_id: r.points
        + after
        - leader.points
        + (0 if _tie_goes_to_leader(leader, r, races_after) else 1)
        for r in rivals
    }
    if next_max < max(need.values()):
        return tm  # can't clinch next time even with a perfect weekend and rivals scoring 0
    tm.can_clinch_next = True

    if team or next_is_sprint:
        tm.clinch_lines = _points_lines(leader, rivals, need, next_max, team, next_is_sprint)
    else:
        tm.clinch_lines = _position_lines(system, rivals, need)
    return tm


def _points_lines(
    leader: Contender,
    rivals: list[Contender],
    need: dict[str, float],
    next_max: int,
    team: bool,
    sprint: bool,
) -> list[str]:
    span = "across the sprint and race" if sprint else "at this race"
    parts = []
    for r in rivals:
        d = need[r.entity_id]
        if d <= -next_max:
            continue  # this rival can't stop it even with a perfect weekend
        if d > 0:
            parts.append(f"outscore {r.name} by at least {fmt_points(d)} points")
        else:
            parts.append(f"avoid being outscored by {r.name} by more than {fmt_points(-d)} points")
    if not parts:
        return ["Clinches regardless of results."]
    return [f"Clinches if they {' and '.join(parts)} {span}."]


def _position_lines(
    system: PointsSystem, rivals: list[Contender], need: dict[str, float]
) -> list[str]:
    fl = system.fastest_lap
    lines: list[str] = []
    unconditional_found = False
    for p in range(1, len(system.race) + 2):  # P1..P10, then "outside the points"
        lead_pts = system.race_points(p)
        conditions = []
        possible = True
        for r in rivals:
            allowed = lead_pts - need[r.entity_id]
            rival_max = (system.race[1] if p == 1 else system.race[0]) + fl
            if allowed >= rival_max:
                continue
            if allowed < 0:
                possible = False
                break
            # Best finishing position the rival may take (worst case: they also get the FL point).
            # (Always found: finishing outside the points scores 0.)
            q = next(
                q
                for q in range(1, len(system.race) + 2)
                if q != p and system.race_points(q) + (fl if q <= 10 else 0) <= allowed
            )
            if q > len(system.race):
                conditions.append(f"{r.name} finishes outside the points")
            else:
                conditions.append(_ordinal_cutoff(r.name, q - 1, p))
        if not possible:
            continue
        what = (
            "with a win"
            if p == 1
            else "even without scoring"
            if p > len(system.race)
            else f"with P{p} or better"
        )
        if not conditions:
            if p > len(system.race):
                lines.append("Clinches regardless of results.")
                break
            unconditional_found = True
            lines = [f"Clinches {what}."]  # supersedes better-position lines
            continue
        lines.append(f"Clinches {what} if {' and '.join(conditions)}.")
        if len(lines) >= 3 or (unconditional_found and len(lines) >= 2):
            break
    return lines[:3]


# ---------------------------------------------------------------------------- DB wiring


def for_season(db: DB, season: int, kind: str, now: datetime | None = None) -> TitleMath | None:
    now = now or utcnow()
    system = system_for(season)
    if system is None:
        return None
    rounds = list(
        db.scalars(
            select(StandingsSnapshot.round)
            .where(StandingsSnapshot.season == season, StandingsSnapshot.type == kind)
            .distinct()
        )
    )
    if not rounds:
        return None
    rnd = max(rounds)
    snaps = list(
        db.scalars(
            select(StandingsSnapshot)
            .where(
                StandingsSnapshot.season == season,
                StandingsSnapshot.type == kind,
                StandingsSnapshot.round == rnd,
            )
            .order_by(StandingsSnapshot.position)
        )
    )
    standings = []
    for s in snaps:
        if kind == "driver":
            d = db.get(Driver, s.entity_id)
            name = d.full_name if d else s.entity_id
        else:
            c = db.get(Constructor, s.entity_id)
            name = c.name if c else s.entity_id
        standings.append(Contender(s.entity_id, name, s.points, s.wins))
    events = list(
        db.scalars(
            select(Event)
            .where(Event.season == season, Event.status == "scheduled")
            .order_by(Event.round)
        )
    )
    if not any(e.round == rnd for e in events):
        return None  # calendar not synced: can't know what's left, so say nothing

    def pending(e: Event, stype: str) -> bool:
        """Still to be run: after the standings round, or (e.g. standings that already include
        a sprint) a session of the standings round that hasn't started yet."""
        sess = next((s for s in e.sessions if s.type == stype), None)
        if sess is None:
            return False
        if e.round > rnd:
            return True
        return e.round == rnd and sess.start_utc is not None and sess.start_utc > now

    remaining = [e for e in events if pending(e, "RACE")]
    sprints = sum(1 for e in events if pending(e, "SPRINT")) if system.sprint else 0
    nxt = remaining[0] if remaining else None
    return compute(
        kind,
        season,
        standings,
        system,
        len(remaining),
        sprints,
        next_event=nxt.name if nxt else None,
        next_is_sprint=bool(nxt and system.sprint and pending(nxt, "SPRINT")),
    )
