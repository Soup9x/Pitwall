"""OpenF1 API (https://openf1.org). Only historical, post-session data is used (2023 onwards)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pitwall.sources.http import CachedClient, RateLimiter

BASE_URL = "https://api.openf1.org/v1"
FIRST_SEASON = 2023
SESSION_NAMES = {"RACE": "Race", "SPRINT": "Sprint"}

# Free tier allows 3 requests/second and 30/minute; stay under both.
_limiter = RateLimiter(min_interval=2.1, per_hour=1500)


def make_client(**kwargs: Any) -> OpenF1Client:
    kwargs.setdefault("limiter", _limiter)
    return OpenF1Client(BASE_URL, **kwargs)


class OpenF1Client(CachedClient):
    def __init__(self, base_url: str = BASE_URL, **kwargs: Any) -> None:
        super().__init__("openf1", base_url, **kwargs)

    def sessions(self, year: int, session_name: str) -> list[dict[str, Any]]:
        return (
            self.get_json(
                "sessions",
                {"year": year, "session_name": session_name},
                max_age=timedelta(hours=12),
                empty_on_404=True,
            )
            or []
        )

    # Completed sessions never change, so these responses are cached forever.
    def laps(self, session_key: int, driver_number: int) -> list[dict[str, Any]]:
        return (
            self.get_json(
                "laps",
                {"session_key": session_key, "driver_number": driver_number},
                max_age=None,
                empty_on_404=True,
            )
            or []
        )

    def stints(self, session_key: int, driver_number: int) -> list[dict[str, Any]]:
        return (
            self.get_json(
                "stints",
                {"session_key": session_key, "driver_number": driver_number},
                max_age=None,
                empty_on_404=True,
            )
            or []
        )

    def location(self, session_key: int, driver_number: int) -> list[dict[str, Any]]:
        """Car positions (x/y/z, ~3.7 Hz) for a whole session. Large, so not cached."""
        return (
            self.get_json(
                "location",
                {"session_key": session_key, "driver_number": driver_number},
                empty_on_404=True,
                store=False,
            )
            or []
        )

    def pits(self, session_key: int, driver_number: int) -> list[dict[str, Any]]:
        return (
            self.get_json(
                "pit",
                {"session_key": session_key, "driver_number": driver_number},
                max_age=None,
                empty_on_404=True,
            )
            or []
        )


def match_session(
    candidates: list[dict[str, Any]], start_utc: datetime, tolerance: timedelta = timedelta(hours=3)
) -> int | None:
    """Find the OpenF1 session_key whose start time matches ours."""
    best: tuple[timedelta, int] | None = None
    for c in candidates:
        try:
            when = datetime.fromisoformat(str(c["date_start"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        diff = abs(when - start_utc)
        if diff <= tolerance and (best is None or diff < best[0]):
            best = (diff, int(c["session_key"]))
    return best[1] if best else None


def summarize(
    laps: list[dict[str, Any]], stints: list[dict[str, Any]], pits: list[dict[str, Any]]
) -> tuple[float | None, int, list[dict[str, Any]]]:
    """(best lap seconds, pit stop count, stints as [{compound, laps, from, to}])."""
    durations = [
        float(lap["lap_duration"]) for lap in laps if lap.get("lap_duration") not in (None, 0)
    ]
    best = min(durations) if durations else None
    out = []
    for s in sorted(stints, key=lambda s: s.get("stint_number") or 0):
        start, end = s.get("lap_start"), s.get("lap_end")
        out.append(
            {
                "compound": (s.get("compound") or "UNKNOWN").upper(),
                "from": start,
                "to": end,
                "laps": (end - start + 1)
                if isinstance(start, int) and isinstance(end, int)
                else None,
                "tyre_age": s.get("tyre_age_at_start"),
            }
        )
    return best, len(pits), out


def fmt_lap(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"
