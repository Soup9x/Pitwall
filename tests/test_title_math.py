"""Title math against real championship deciders."""

from __future__ import annotations

from pitwall.services.points import system_for
from pitwall.services.title_math import Contender, compute


def C(name, points, wins):
    return Contender(name.lower(), name, points, wins)


def test_points_systems():
    assert system_for(2002) is None
    s08 = system_for(2008)
    assert s08.race_max == 10 and s08.sprint_max == 0
    s21 = system_for(2021)
    assert (s21.race_max, s21.sprint_max, s21.team_race_max) == (26, 3, 44)
    s23 = system_for(2023)
    assert (s23.race_max, s23.sprint_max, s23.team_sprint_max) == (26, 8, 15)
    s26 = system_for(2026)
    assert (s26.race_max, s26.fastest_lap) == (25, 0)  # fastest-lap point dropped from 2025


def test_2008_brazil_hamilton_needs_fifth():
    """Before the 2008 finale Hamilton had 94 points to Massa's 87 (10-8-6-5-4-3-2-1)."""
    tm = compute(
        "driver",
        2008,
        [C("Hamilton", 94, 5), C("Massa", 87, 5)],
        system_for(2008),
        remaining_races=1,
        remaining_sprints=0,
        next_event="Brazilian Grand Prix",
    )
    assert not tm.champion and tm.can_clinch_next
    assert [c.name for c in tm.alive] == ["Hamilton", "Massa"]
    assert tm.clinch_lines[0] == "Clinches with P5 or better."
    # He finished 5th; Massa won. Had he been 6th he'd have needed Massa not to win.
    assert tm.clinch_lines[1] == "Clinches with P6 or better if Massa doesn't win."


def test_2016_abu_dhabi_rosberg_needs_podium():
    """Rosberg led Hamilton 367-355 going into the 2016 finale."""
    tm = compute(
        "driver",
        2016,
        [C("Rosberg", 367, 9), C("Hamilton", 355, 9)],
        system_for(2016),
        1,
        0,
        next_event="Abu Dhabi Grand Prix",
    )
    assert tm.clinch_lines[0] == "Clinches with P3 or better."
    assert tm.clinch_lines[1] == "Clinches with P4 or better if Hamilton doesn't win."


def test_2021_abu_dhabi_level_on_points():
    """Verstappen and Hamilton were level on 369.5 before the 2021 finale."""
    tm = compute(
        "driver",
        2021,
        [C("Verstappen", 369.5, 9), C("Hamilton", 369.5, 8), C("Bottas", 218, 1)],
        system_for(2021),
        1,
        0,
        next_event="Abu Dhabi Grand Prix",
    )
    assert [c.name for c in tm.alive] == ["Verstappen", "Hamilton"]
    assert [c.name for c in tm.eliminated] == ["Bottas"]
    assert tm.relevant and tm.can_clinch_next
    assert tm.clinch_lines[0] == "Clinches with a win."
    assert tm.clinch_lines[1] == "Clinches with P2 or better if Hamilton finishes behind them."


def test_2022_japan_verstappen_clinch_conditions_and_result():
    """Before Suzuka 2022: Verstappen 341 (11 wins), Leclerc 237, Perez 235, Russell 203,
    Sainz 202; five races and one sprint (Brazil) left."""
    standings = [
        C("Verstappen", 341, 11),
        C("Leclerc", 237, 3),
        C("Perez", 235, 2),
        C("Russell", 203, 0),
        C("Sainz", 202, 1),
    ]
    tm = compute(
        "driver", 2022, standings, system_for(2022), 5, 1, next_event="Japanese Grand Prix"
    )
    assert tm.max_remaining == 5 * 26 + 8
    # Russell could only tie on points (341) and would lose the countback; Sainz can't reach it.
    assert [c.name for c in tm.eliminated] == ["Russell", "Sainz"]
    assert tm.can_clinch_next and not tm.champion
    # Verstappen won and Leclerc was demoted to P3: champion.
    assert tm.clinch_lines[0] == "Clinches with a win if Leclerc finishes below P2."
    assert tm.clinch_lines[1].startswith("Clinches with P2 or better if Leclerc finishes below P5")

    after = compute(
        "driver",
        2022,
        [C("Verstappen", 366, 12), C("Perez", 253, 2), C("Leclerc", 252, 3)],
        system_for(2022),
        4,
        1,
        next_event="United States Grand Prix",
    )
    assert after.champion.name == "Verstappen"
    assert after.summary() == ["Verstappen has clinched the 2022 Drivers' title."]


def test_2023_qatar_sprint_weekend():
    """After Japan 2023 Verstappen had 400 points to Perez's 223, with six races and three
    sprints left; he wrapped it up in the Qatar sprint."""
    standings = [C("Verstappen", 400, 13), C("Perez", 223, 2), C("Hamilton", 190, 0)]
    tm = compute(
        "driver",
        2023,
        standings,
        system_for(2023),
        6,
        3,
        next_event="Qatar Grand Prix",
        next_is_sprint=True,
    )
    assert not tm.champion and tm.can_clinch_next
    assert tm.clinch_lines == [
        "Clinches if they avoid being outscored by Perez by more than 31 points "
        "across the sprint and race."
    ]
    # A round earlier (seven races, four sprints left) it wasn't possible yet.
    earlier = compute(
        "driver",
        2023,
        [C("Verstappen", 374, 12), C("Perez", 223, 2)],
        system_for(2023),
        7,
        4,
        next_event="Japanese Grand Prix",
    )
    assert not earlier.can_clinch_next


def test_constructors_use_points_phrasing():
    tm = compute(
        "constructor",
        2022,
        [C("Red Bull", 576, 15), C("Ferrari", 416, 4)],
        system_for(2022),
        5,
        1,
        next_event="Japanese Grand Prix",
    )
    assert tm.max_remaining == 5 * 44 + 15
    # 160 ahead; 191 left after this race -> must extend the lead by 31 (max per race is 44).
    assert tm.clinch_lines == [
        "Clinches if they outscore Ferrari by at least 31 points at this race."
    ]
    tm = compute(
        "constructor",
        2022,
        [C("Red Bull", 619, 16), C("Ferrari", 457, 4)],
        system_for(2022),
        4,
        1,
        next_event="United States Grand Prix",
    )
    # 162 ahead; 147 left afterwards -> safe unless Ferrari gains more than 15.
    assert tm.clinch_lines == [
        "Clinches if they avoid being outscored by Ferrari by more than 15 points at this race."
    ]


def test_early_season_not_relevant():
    standings = [C("A", 25, 1), C("B", 18, 0), C("C", 0, 0)]
    tm = compute("driver", 2026, standings, system_for(2026), 23, 6, next_event="X GP")
    assert len(tm.alive) == 3 and not tm.relevant and not tm.can_clinch_next
