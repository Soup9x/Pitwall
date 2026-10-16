"""Season progression chart: cumulative points per round from stored standings snapshots,
gaps (never estimates) for rounds not fetched yet, and a background backfill from Jolpica."""

from __future__ import annotations

import copy
import json
import re

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall import jobs
from pitwall.models import FollowedDriver, StandingsSnapshot
from pitwall.services import progression
from pitwall.services.sync import apply_results, apply_schedule, apply_standings
from pitwall.settings_store import get_kv
from pitwall.sources.http import RateLimiter
from pitwall.sources.jolpica import JolpicaClient
from pitwall.web import auth
from tests.conftest import dt, load, races

AFTER = dt("2025-10-28T12:00:00")  # all three fixture rounds are done


def _list(name: str) -> dict:
    return load(name)["MRData"]["StandingsTable"]["StandingsLists"][0]


@pytest.fixture
def season(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for f in ("driver_standings_2025_2.json", "driver_standings_2025_3.json"):
        apply_standings(db, "driver", _list(f))
    apply_standings(db, "constructor", _list("constructor_standings_2025_3.json"))
    for race in races("results_2025_3.json"):  # tells us who drives for whom
        apply_results(db, race, "RACE", now=dt("2025-10-26T22:00:00"))
    db.add(FollowedDriver(driver_id="leclerc"))
    db.commit()


def test_build_uses_snapshots_and_leaves_gaps(db, season):
    data = progression.build(db, 2025, "driver", user_id=1, now=AFTER)
    assert [r["round"] for r in data["rounds"]] == [1, 2, 3]
    assert [r["label"] for r in data["rounds"]] == ["MEL", "SHA", "MEX"]
    assert [r["sprint"] for r in data["rounds"]] == [False, True, False]
    assert data["missing"] == [1]
    by_id = {s["id"]: s for s in data["series"]}
    assert by_id["piastri"]["points"] == [None, 44.0, 56.0]  # round 1: a gap, not a guess
    assert [s["id"] for s in data["series"][:2]] == ["norris", "piastri"]  # final order
    assert by_id["leclerc"]["followed"] and not by_id["norris"]["followed"]
    assert by_id["leclerc"]["code"] == "LEC" and by_id["leclerc"]["color"] == "#E8002D"

    teams = progression.build(db, 2025, "constructor", user_id=1, now=AFTER)
    assert teams["missing"] == [1, 2]
    ferrari = next(s for s in teams["series"] if s["id"] == "ferrari")
    assert ferrari["followed"]  # a followed driver drives for them


def test_build_stops_before_a_held_round_and_unfinished_races(db, season):
    held = progression.build(db, 2025, "driver", user_id=1, upto_round=2, now=AFTER)
    assert [r["round"] for r in held["rounds"]] == [1, 2]
    assert "56.0" not in json.dumps(held) and "65.0" not in json.dumps(held)
    # An hour after the Mexico race the standings aren't settled: round 3 isn't shown yet.
    early = progression.build(db, 2025, "driver", user_id=1, now=dt("2025-10-26T21:30:00"))
    assert [r["round"] for r in early["rounds"]] == [1, 2]


def _client(handler) -> JolpicaClient:
    return JolpicaClient(
        "https://api.jolpi.ca/ergast/f1",
        limiter=RateLimiter(),
        sleep=lambda _s: None,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


def _round1() -> dict:
    lst = copy.deepcopy(_list("driver_standings_2025_2.json"))
    lst["round"] = "1"
    return lst


def _wrap(lst: dict) -> dict:
    return {"MRData": {"StandingsTable": {"StandingsLists": [lst]}}}


def test_backfill_stores_only_the_round_it_asked_for(db, season):
    calls = []

    def handler(request):
        path = request.url.path.split("/ergast/f1/", 1)[-1]
        calls.append(path)
        if path == "2025/1/driverStandings.json":
            return httpx.Response(200, json=_wrap(_round1()))
        if path == "2025/1/constructorStandings.json":  # API answered with a different round
            return httpx.Response(200, json=_wrap(_list("constructor_standings_2025_3.json")))
        return httpx.Response(503)

    made = progression.backfill(_client(handler), 2025, AFTER)
    assert calls[:2] == ["2025/1/driverStandings.json", "2025/1/constructorStandings.json"]
    assert made == 3  # the 503 on round 2 stopped the run
    db.expire_all()
    assert progression.missing_rounds(db, 2025, AFTER) == {"driver": [], "constructor": [1, 2]}
    rounds = set(
        db.scalars(select(StandingsSnapshot.round).where(StandingsSnapshot.type == "driver"))
    )
    assert rounds == {1, 2, 3}


def test_backfill_respects_the_request_limit(db, season):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"MRData": {"StandingsTable": {"StandingsLists": []}}})

    assert progression.backfill(_client(handler), 2025, AFTER, limit=2) == 2
    assert len(calls) == 2


def test_queue_and_job(db, season, api):
    routes, _factory = api
    progression.request(db, 2025)
    progression.request(db, 2025)
    db.commit()
    assert get_kv(db, progression.QUEUE_KEY) == [2025]
    assert progression.pending_seasons(db, AFTER) == [2025]
    routes.set("2025/1/driverStandings.json", _wrap(_round1()))
    lst = copy.deepcopy(_list("constructor_standings_2025_3.json"))
    for rnd in (1, 2):
        routes.set(f"2025/{rnd}/constructorStandings.json", _wrap({**lst, "round": str(rnd)}))
    jobs.progression_job(now=AFTER)
    db.expire_all()
    assert progression.missing_rounds(db, 2025, AFTER) == {"driver": [], "constructor": []}
    assert get_kv(db, progression.QUEUE_KEY) == []  # done: off the queue
    calls = len(routes.calls)
    jobs.progression_job(now=AFTER)
    assert len(routes.calls) == calls  # nothing missing, nothing fetched


def test_job_keeps_what_it_has_when_jolpica_is_down(db, season):
    jobs.progression_job(now=AFTER)  # conftest: every request answers 503
    db.expire_all()
    assert progression.missing_rounds(db, 2025, AFTER)["driver"] == [1]


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def test_standings_page_embeds_chart_data_and_queues_missing_rounds(db, client, season):
    with time_machine.travel(AFTER, tick=False):
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
        client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        html = client.get("/standings?season=2025").text
    assert 'data-chart="progression"' in html and 'data-prog="gap"' in html
    blob = re.search(
        r'<script type="application/json" id="progression-data">(.*?)</script>', html, re.S
    )
    data = json.loads(blob[1])
    assert data["driver"]["missing"] == [1] and data["constructor"]["missing"] == [1, 2]
    assert "Downloading round" in html
    assert '<script src="/static/vendor/chart.umd.min.js"></script>' in html
    db.expire_all()
    assert get_kv(db, progression.QUEUE_KEY) == [2025]
