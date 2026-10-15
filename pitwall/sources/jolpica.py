"""Jolpica F1 API (Ergast-compatible). https://github.com/jolpica/jolpica-f1"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pitwall.config import get_env
from pitwall.sources.http import CachedClient, RateLimiter

PAGE = 100

# Jolpica documents 4 req/s burst and 500 req/hour sustained for unauthenticated use.
_limiter = RateLimiter(min_interval=0.3, per_hour=450)


def make_client(**kwargs: Any) -> JolpicaClient:
    kwargs.setdefault("limiter", _limiter)
    return JolpicaClient(get_env().jolpica_base_url, **kwargs)


class JolpicaClient(CachedClient):
    def __init__(self, base_url: str, **kwargs: Any) -> None:
        super().__init__("jolpica", base_url, **kwargs)

    def _races(self, path: str, max_age: timedelta | None) -> list[dict[str, Any]]:
        """Fetch all pages of a RaceTable endpoint and merge rows belonging to the same race."""
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        offset = 0
        while True:
            data = self.get_json(path, {"limit": PAGE, "offset": offset}, max_age=max_age)
            mr = data["MRData"]
            races = mr.get("RaceTable", {}).get("Races", [])
            for race in races:
                key = f"{race['season']}-{race['round']}"
                if key not in merged:
                    merged[key] = race
                    order.append(key)
                    continue
                for list_key in ("Results", "QualifyingResults", "SprintResults"):
                    if list_key in race:
                        merged[key].setdefault(list_key, []).extend(race[list_key])
            total = int(mr.get("total", 0))
            offset += int(mr.get("limit", PAGE))
            if offset >= total or not races:
                break
        return [merged[k] for k in order]

    def schedule(self, year: int, max_age: timedelta | None = timedelta(0)) -> list[dict[str, Any]]:
        return self._races(f"{year}.json", max_age)

    def results(
        self, year: int, rnd: int | None = None, max_age: timedelta | None = timedelta(0)
    ) -> list[dict[str, Any]]:
        path = f"{year}/{rnd}/results.json" if rnd else f"{year}/results.json"
        return self._races(path, max_age)

    def qualifying(
        self, year: int, rnd: int | None = None, max_age: timedelta | None = timedelta(0)
    ) -> list[dict[str, Any]]:
        path = f"{year}/{rnd}/qualifying.json" if rnd else f"{year}/qualifying.json"
        return self._races(path, max_age)

    def sprint(
        self, year: int, rnd: int | None = None, max_age: timedelta | None = timedelta(0)
    ) -> list[dict[str, Any]]:
        path = f"{year}/{rnd}/sprint.json" if rnd else f"{year}/sprint.json"
        return self._races(path, max_age)

    def standings(
        self, kind: str, year: int, rnd: int | None = None, max_age: timedelta | None = timedelta(0)
    ) -> dict[str, Any] | None:
        """kind: 'driver' or 'constructor'. Returns the StandingsList dict or None."""
        endpoint = "driverStandings" if kind == "driver" else "constructorStandings"
        path = f"{year}/{rnd}/{endpoint}.json" if rnd else f"{year}/{endpoint}.json"
        data = self.get_json(path, {"limit": PAGE}, max_age=max_age)
        lists = data["MRData"].get("StandingsTable", {}).get("StandingsLists", [])
        return lists[0] if lists else None
