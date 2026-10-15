"""Trivia generators: every fact must follow from the stored results."""

from __future__ import annotations

from datetime import date

import pytest

from pitwall.models import Event
from pitwall.services import trivia
from pitwall.services.trivia import Fact, pick
from tests.world import World


@pytest.fixture
def w(db):
    w = World(db)
    w.circuit("testford", "UK", name="Testford Park")
    w.circuit("filler", "Nowhere")
    w.con("redline", "British", "Redline")
    w.con("azure", "Italian", "Azure")
    return w


def facts_of(db, event, type_=None):
    return [f for f in trivia.generate(db, event) if type_ is None or f.type == type_]


def texts(facts):
    return [f.text for f in facts]


def test_50th_start_only_when_career_is_fully_loaded(db, w):
    w.driver("alice_ace", "British", date(1999, 5, 1))  # debuts 2020 (age 20)
    w.driver("vic_vet", "Spanish", date(1990, 1, 1))  # already racing in 2019 = first loaded
    w.season(2019, 2020, 2021, 2022, 2023, 2024, 2025)
    ev = w.event(2019, 1, "filler")
    w.results(ev, "RACE", [("vic_vet", "azure", 1, 1, 25, "Finished")])
    starts = 0
    for year in range(2020, 2025):
        for rnd in range(1, 11):
            ev = w.event(year, rnd, "filler")
            status = "Did not start" if (year, rnd) == (2022, 5) else "Finished"
            w.results(
                ev,
                "RACE",
                [
                    ("alice_ace", "redline", 2, 2, 18, status),
                    ("vic_vet", "azure", 1, 1, 25, "Finished"),
                ],
            )
            starts += status == "Finished"
    assert starts == 49
    w.standings(2024, 10, [("vic_vet", 250, 10), ("alice_ace", 180, 0)])
    target = w.event(2025, 1, "testford")
    found = texts(facts_of(db, target, "milestone"))
    assert "Alice Ace is set to make their 50th Grand Prix start." in found
    # Vic Vet's career may predate the loaded data, so no career counts are stated.
    assert not any("Vic Vet is set to" in t for t in found)


def test_first_win_and_circuit_record(db, w):
    w.driver("alice_ace", "British", date(2000, 1, 1))
    w.driver("bob_bolt", "Dutch", date(1998, 1, 1))
    w.driver("vic_vet", "Spanish", date(1990, 1, 1))
    w.season(2018)
    w.results(w.event(2018, 1, "filler"), "RACE", [("vic_vet", "azure", 1, 1, 25, "Finished")])
    winners = {
        2019: "vic_vet",
        2020: "vic_vet",
        2021: "vic_vet",
        2022: "alice_ace",
        2023: "alice_ace",
        2024: "vic_vet",
    }
    for year, winner in winners.items():
        ev = w.event(year, 1, "testford")
        rows = [(winner, "redline", 1, 3, 25, "Finished")]
        others = [d for d in ("alice_ace", "vic_vet") if d != winner]
        rows += [(others[0], "azure", 3, 1, 15, "Finished")]
        if year >= 2020:
            rows.append(("bob_bolt", "azure", 2 if year == 2024 else 6, 5, 18, "Finished"))
        w.results(ev, "RACE", rows)
    w.standings(2024, 1, [("vic_vet", 25, 1), ("bob_bolt", 18, 0), ("alice_ace", 15, 0)])
    target = w.event(2025, 1, "testford")
    found = texts(facts_of(db, target, "milestone"))
    assert (
        "Bob Bolt is still chasing a first Grand Prix win after 5 starts — they were P2 "
        "at the 2024 Testford GP."
    ) in found
    assert "Vic Vet has won at Testford Park 4 times — more than anyone since 2018." in found
    assert not any("level with" in t for t in found)  # Alice has 2; record is 4


def test_streaks_and_at_least(db, w):
    w.driver("alice_ace", "British", date(2000, 1, 1))
    w.driver("bob_bolt", "Dutch", date(1998, 1, 1))
    w.season(2024, 2025)
    for rnd in range(1, 7):
        ev = w.event(2025, rnd, "filler")
        w.results(
            ev,
            "RACE",
            [
                ("alice_ace", "redline", 1 if rnd > 3 else 4, 1 if rnd > 2 else 5, 25, "Finished"),
                (
                    "bob_bolt",
                    "azure",
                    15 if rnd < 5 else 18,
                    9,
                    0,
                    "Finished" if rnd < 5 else "Engine",
                ),
            ],
        )
    w.standings(2025, 6, [("alice_ace", 150, 3), ("bob_bolt", 0, 0)])
    target = w.event(2025, 7, "testford")
    found = texts(facts_of(db, target, "streak"))
    # Alice's history starts in 2025, the first loaded season, so a whole-history streak is
    # only "at least".
    assert "Alice Ace has scored points in at least 6 consecutive races." in found
    assert "Alice Ace has finished on the podium in 3 consecutive races." in found
    assert "Alice Ace has started from the front row in 4 consecutive races." in found
    assert "Bob Bolt has failed to finish the last 2 races." in found


def test_on_this_day_and_home_race(db, w):
    w.driver("alice_ace", "British", date(2000, 1, 1))
    w.driver("old_timer", "German", date(1970, 1, 1))
    w.season(2014, 2015, 2016, 2020, 2024)
    w.results(
        w.event(2015, 1, "testford", "British Grand Prix"),
        "RACE",
        [("old_timer", "azure", 1, 3, 25, "Finished")],
    )
    w.results(
        w.event(2016, 1, "testford", "British Grand Prix"),
        "RACE",
        [("old_timer", "azure", 1, 1, 25, "Finished")],
    )
    w.results(
        w.event(2024, 1, "testford", "British Grand Prix"),
        "RACE",
        [
            ("alice_ace", "redline", 2, 4, 18, "Finished"),
            ("old_timer", "redline", 1, 1, 25, "Finished"),
        ],
    )
    w.standings(2024, 1, [("alice_ace", 18, 0)], [("redline", 43, 1)])
    target = w.event(2025, 1, "testford", "British Grand Prix")
    otd = texts(facts_of(db, target, "on_this_day"))
    assert otd == ["10 years ago, Old Timer (Azure) won the 2015 British Grand Prix from P3."]
    home = texts(facts_of(db, target, "home_race"))
    assert (
        "Home race for Alice Ace (British): their best finish at Testford Park is P2 (2024)."
    ) in home
    assert "Home race for Redline: 1 win at Testford Park since 2015." in home


def test_rookie_and_ages(db, w):
    w.driver("young_gun", "Mexican", date(2007, 3, 3))
    w.driver("old_hand", "Spanish", date(1981, 7, 29))
    w.season(2024, 2025)
    w.results(w.event(2024, 1, "filler"), "RACE", [("old_hand", "azure", 3, 3, 15, "Finished")])
    for rnd in (1, 2):
        w.results(
            w.event(2025, rnd, "filler"),
            "RACE",
            [
                ("young_gun", "redline", 7 - rnd, 8, 8, "Finished"),
                ("old_hand", "azure", 3, 3, 15, "Finished"),
            ],
        )
    w.standings(2025, 2, [("old_hand", 30, 0), ("young_gun", 18, 0)])
    target = w.event(2025, 3, "testford")  # race on 2025-03-22
    found = texts(facts_of(db, target, "rookie_veteran"))
    assert "Young Gun is the youngest driver on the grid at 18 years and 19 days." in found
    assert "Old Hand is the oldest driver on the grid at 43 years and 236 days." in found
    assert "Rookie Young Gun: 2 starts and 16 points so far, best finish P5." in found


def test_every_fact_has_a_source(db, w):
    test_on_this_day_and_home_race(db, w)
    target = db.query(Event).filter(Event.season == 2025).one()
    for f in trivia.generate(db, target):
        assert (
            f.source
            and f.key
            and f.type in {"milestone", "streak", "on_this_day", "home_race", "rookie_veteran"}
        )


def test_pick_ranks_caps_per_type_and_avoids_recent():
    facts = [Fact("streak", f"s{i}", f"streak {i}", 50 + i, "src") for i in range(4)]
    facts += [Fact("home_race", "h", "home", 10, "src"), Fact("milestone", "m", "ms", 90, "src")]
    chosen = pick(facts)
    assert [f.key for f in chosen] == ["m", "s3", "s2", "h"]  # max 2 streaks
    assert [f.key for f in pick(facts, exclude={"m", "s3"})] == ["s2", "s1", "h"]


def test_recent_previews_are_remembered(db):
    trivia.remember_keys(db, ["a", "b"])
    trivia.remember_keys(db, ["c"])
    trivia.remember_keys(db, ["d"])
    assert trivia.recent_keys(db) == {"a", "b", "c", "d"}
    trivia.remember_keys(db, ["e"])  # only the last 3 previews count
    assert trivia.recent_keys(db) == {"c", "d", "e"}
