"""Championship points systems by season (for title math)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PointsSystem:
    race: tuple[int, ...]
    sprint: tuple[int, ...] = ()
    fastest_lap: int = 0  # bonus for a classified top-10 finisher with the fastest lap

    def race_points(self, position: int) -> int:
        return self.race[position - 1] if 1 <= position <= len(self.race) else 0

    @property
    def race_max(self) -> int:
        return self.race[0] + self.fastest_lap

    @property
    def sprint_max(self) -> int:
        return self.sprint[0] if self.sprint else 0

    @property
    def team_race_max(self) -> int:  # both cars 1-2, plus fastest lap
        return self.race[0] + self.race[1] + self.fastest_lap

    @property
    def team_sprint_max(self) -> int:
        return self.sprint[0] + self.sprint[1] if len(self.sprint) > 1 else self.sprint_max


_POINTS_2010 = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)


def system_for(season: int) -> PointsSystem | None:
    """The points system for a season, or None for seasons title math doesn't model
    (pre-2003 seasons had dropped scores; 2014's double-points finale is ignored)."""
    if season < 2003:
        return None
    if season < 2010:
        return PointsSystem((10, 8, 6, 5, 4, 3, 2, 1))
    fastest = 1 if 2019 <= season <= 2024 else 0
    if season == 2021:
        sprint: tuple[int, ...] = (3, 2, 1)
    elif season >= 2022:
        sprint = (8, 7, 6, 5, 4, 3, 2, 1)
    else:
        sprint = ()
    return PointsSystem(_POINTS_2010, sprint, fastest)
