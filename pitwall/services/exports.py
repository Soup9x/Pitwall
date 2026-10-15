"""CSV exports: standings and followed-driver race-by-race stats."""

from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.models import OWNER_ID, FollowedDriver
from pitwall.services import driver_stats, standings


def _csv(header: list[str], rows: list[list[object]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def standings_csv(db: DB, season: int, kind: str) -> str | None:
    table = standings.table(db, season, kind)
    if table is None:
        return None
    header = ["season", "round", "position", "id", "name"]
    header += ["team"] if kind == "driver" else []
    header += ["points", "wins", "gap_to_leader", "gap_to_car_ahead", "change_since_last_round"]
    rows = []
    for r in table.rows:
        row: list[object] = [season, table.round, r.position, r.entity_id, r.name]
        if kind == "driver":
            row.append(r.team or "")
        row += [
            r.points,
            r.wins,
            r.gap_leader if r.gap_leader is not None else "",
            r.gap_ahead if r.gap_ahead is not None else "",
            r.change if r.change is not None else "",
        ]
        rows.append(row)
    return _csv(header, rows)


def drivers_csv(
    db: DB, season: int, driver_ids: list[str] | None = None, user_id: int = OWNER_ID
) -> str:
    """One row per race for each driver (default: the user's followed drivers)."""
    if driver_ids is None:
        driver_ids = [
            f.driver_id
            for f in db.scalars(select(FollowedDriver).where(FollowedDriver.user_id == user_id))
        ]
    header = [
        "season",
        "round",
        "grand_prix",
        "driver_id",
        "driver",
        "team",
        "grid",
        "finish",
        "positions_gained",
        "status",
        "dnf",
        "sprint",
        "points",
        "cumulative_points",
        "best_lap",
        "pit_stops",
        "tyre_stints",
    ]
    rows: list[list[object]] = []
    for did in driver_ids:
        data = driver_stats.season(db, did, season)
        if data is None:
            continue
        for r in data.rows:
            stints = " ".join(
                f"{s['compound']}({s['laps']})" if s.get("laps") else s["compound"]
                for s in r.stints
            )
            rows.append(
                [
                    season,
                    r.event.round,
                    r.event.name,
                    did,
                    data.driver.full_name,
                    r.team or "",
                    r.grid or "",
                    r.finish,
                    r.gained if r.gained is not None else "",
                    r.status,
                    "yes" if r.dnf else "no",
                    r.sprint or "",
                    r.points,
                    r.cumulative,
                    r.best_lap or "",
                    r.pit_count if r.pit_count is not None else "",
                    stints,
                ]
            )
    return _csv(header, rows)
