"""ICS feed: stable UIDs, SEQUENCE bumps, filtering."""

from __future__ import annotations

import copy

from icalendar import Calendar

from pitwall.services import ics
from pitwall.services.sync import apply_schedule
from pitwall.settings_store import load_settings
from tests.conftest import dt, races

NOW = dt("2025-03-01T00:00:00")


def _events(db, types=None):
    cal = Calendar.from_ical(ics.build_calendar(db, load_settings(db), types, now=NOW))
    return {str(e["uid"]): e for e in cal.walk("VEVENT")}


def test_feed_contents(db):
    apply_schedule(
        db,
        2025,
        races("schedule_2025.json"),
        now=NOW,
        curated={"rodriguez": {"timezone": "America/Mexico_City"}},
    )
    evs = _events(db)
    assert len(evs) == 15
    race = evs["2025-rodriguez-race@pitwall"]
    assert str(race["summary"]) == "🏁 Race — Mexico City GP"
    assert str(race["location"]) == "Autódromo Hermanos Rodríguez"
    assert race.decoded("dtstart") == dt("2025-10-26T20:00:00")
    assert race.decoded("dtend") == dt("2025-10-26T22:00:00")
    desc = str(race["description"])
    assert "Local time: Sun 26 Oct, 2:00 PM CST (Mexico City)" in desc
    assert "Your time: Sun 26 Oct, 4:00 PM EDT" in desc
    assert "https://pitwall.example/events/" in desc
    assert int(race["sequence"]) == 0


def test_uids_stable_and_sequence_increments_on_change(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    before = _events(db)
    apply_schedule(db, 2025, data, now=NOW, curated={})
    assert _events(db).keys() == before.keys()

    moved = copy.deepcopy(data)
    moved[2]["time"] = "21:00:00Z"
    del moved[0]["FirstPractice"]
    apply_schedule(db, 2025, moved, now=NOW, curated={})
    after = _events(db)
    assert after.keys() == before.keys()  # same UIDs: updates, not duplicates
    race = after["2025-rodriguez-race@pitwall"]
    assert int(race["sequence"]) == 1
    assert race.decoded("dtstart") == dt("2025-10-26T21:00:00")
    fp1 = after["2025-albert_park-fp1@pitwall"]
    assert str(fp1["status"]) == "CANCELLED" and int(fp1["sequence"]) == 1
    assert int(after["2025-rodriguez-q@pitwall"]["sequence"]) == 0


def test_uids_survive_round_renumbering(db):
    data = races("schedule_2025.json")
    apply_schedule(db, 2025, data, now=NOW, curated={})
    renumbered = copy.deepcopy(data)
    del renumbered[1]
    renumbered[1]["round"] = "2"
    apply_schedule(db, 2025, renumbered, now=NOW, curated={})
    assert "2025-rodriguez-race@pitwall" in _events(db)


def test_filter(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=NOW, curated={})
    assert ics.parse_filter("race,q") == {"RACE", "Q"}
    assert ics.parse_filter("practice") == {"FP1", "FP2", "FP3"}
    assert ics.parse_filter("bogus") is None
    assert ics.parse_filter(None) is None
    evs = _events(db, ics.parse_filter("race,q"))
    assert len(evs) == 6
    assert all(u.endswith(("-race@pitwall", "-q@pitwall")) for u in evs)
    sprint = _events(db, ics.parse_filter("sprint"))
    assert list(sprint) == ["2025-shanghai-sprint@pitwall"]
