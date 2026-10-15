"""Parses real recorded Jolpica responses, if any were captured with scripts/record_fixtures.py."""

from __future__ import annotations

import json

import pytest

from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from tests.conftest import FIXTURES

RECORDED = sorted((FIXTURES / "recorded").glob("schedule_*.json"))


@pytest.mark.skipif(not RECORDED, reason="no recorded fixtures (run scripts/record_fixtures.py)")
@pytest.mark.parametrize("schedule", RECORDED, ids=lambda p: p.stem)
def test_recorded_season_parses(db, schedule):
    year = int(schedule.stem.split("_")[1])
    races = json.loads(schedule.read_text())["MRData"]["RaceTable"]["Races"]
    res = apply_schedule(db, year, races, curated={})
    assert res.events == len(races) > 0
    for kind, stype in (("results", "RACE"), ("qualifying", "Q")):
        f = FIXTURES / "recorded" / f"{kind}_{year}_last.json"
        if f.exists():
            for race in json.loads(f.read_text())["MRData"]["RaceTable"]["Races"]:
                assert apply_results(db, race, stype) is not None
    for kind in ("driver", "constructor"):
        f = FIXTURES / "recorded" / f"{kind}_standings_{year}.json"
        if f.exists():
            lists = json.loads(f.read_text())["MRData"]["StandingsTable"]["StandingsLists"]
            if lists:
                assert apply_standings(db, kind, lists[0]).changed
