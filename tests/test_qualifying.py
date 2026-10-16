"""Qualifying breakdown: eliminations derived from the times, gaps, purple sectors, grid
changes, the event page tab (spoiler-safe) and the driver page's qualifying stats."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, FollowedDriver, Result, Session
from pitwall.services import qualifying
from pitwall.services.sync import apply_results, apply_schedule
from pitwall.settings_store import save_settings
from pitwall.web import auth
from tests.conftest import dt, races

QUALI_AT = dt("2025-10-25T23:30:00")
RACE_AT = dt("2025-10-26T22:00:00")
LATER = RACE_AT + timedelta(days=3)


@pytest.fixture
def mexico(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for race in races("qualifying_2025_3_grid22.json"):
        apply_results(db, race, "Q", now=QUALI_AT)
    db.add(FollowedDriver(driver_id="driver02"))
    db.commit()
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


def _q(event):
    return next(s for s in event.sessions if s.type == "Q")


def test_parse_time():
    assert qualifying.parse_time("1:15.912") == pytest.approx(75.912)
    assert qualifying.parse_time("59.1") == pytest.approx(59.1)
    for bad in ("", None, "DNF", "1:xx.1"):
        assert qualifying.parse_time(bad) is None
    assert qualifying.fmt_time(75.5) == "1:15.500"


def test_groups_come_from_the_times_not_fixed_counts(db, mexico):
    b = qualifying.breakdown(db, _q(mexico))
    assert [(name, len(rows)) for name, rows in b.groups] == [
        ("Q3", 10),
        ("Out in Q2", 6),
        ("Out in Q1", 6),  # 22 cars: no assumption of 20
    ]
    rows = {r.driver.id: r for r in b.rows}
    assert rows["driver22"].reached == 1 and rows["driver22"].times == [None, None, None]
    assert rows["driver22"].gap_pole is None and rows["driver22"].gap_cutoff is None


def test_fastest_per_segment_and_gaps(db, mexico):
    b = qualifying.breakdown(db, _q(mexico))
    rows = {r.driver.id: r for r in b.rows}
    # Purple goes to the fastest time in each segment, which isn't always the pole sitter.
    assert rows["driver03"].fastest == [True, False, False]
    assert rows["driver02"].fastest == [False, True, False]
    assert rows["driver01"].fastest == [False, False, True]
    assert b.pole is rows["driver01"]
    assert rows["driver02"].gap_pole == pytest.approx(0.1)
    assert rows["driver02"].gap_pole_pct == pytest.approx(0.13)
    # Cut-off: the slowest time that still went through (P16 in Q1, P10 in Q2).
    assert b.cutoff_text(0) == "1:17.000" and b.cutoff_text(1) == "1:16.350"
    assert rows["driver17"].gap_cutoff == pytest.approx(0.1)
    assert rows["driver11"].gap_cutoff == pytest.approx(0.15)
    assert rows["driver05"].gap_cutoff is None  # went through
    assert rows["driver01"].improvements == [("Q1→Q2", -0.4), ("Q2→Q3", -0.4)]
    assert rows["driver17"].improvements == []


def test_teammates_compared_in_their_last_common_segment(db, mexico):
    rows = {r.driver.id: r for r in qualifying.breakdown(db, _q(mexico)).rows}
    assert (rows["driver11"].teammate_gap, rows["driver11"].teammate_segment) == (-0.1, "Q2")
    assert rows["driver12"].teammate_gap == pytest.approx(0.1)
    assert (rows["driver02"].teammate_gap, rows["driver02"].teammate_segment) == (-1.25, "Q1")
    assert rows["driver01"].teammate_gap is None  # teammate set no time at all


def test_grid_shown_only_where_it_differs(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for race in races("qualifying_2025_3.json"):
        apply_results(db, race, "Q", now=QUALI_AT)
    ev = db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()
    b = qualifying.breakdown(db, _q(ev))
    assert not b.has_grid  # no race yet
    assert [name for name, _ in b.groups] == ["Q3"]  # all three reached Q3
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=RACE_AT)
    rows = {r.driver.id: r for r in qualifying.breakdown(db, _q(ev)).rows}
    assert rows["norris"].grid_text is None
    assert (rows["leclerc"].grid_text, rows["hamilton"].grid_text) == ("P3", "P2")


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _get(client, path, at=LATER):
    with time_machine.travel(at, tick=False):
        client.cookies.clear()
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)[1]
        client.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        return client.get(path).text


def test_event_page_qualifying_tab(client, mexico):
    overview = _get(client, f"/events/{mexico.id}")
    assert f'href="/events/{mexico.id}?tab=qualifying"' in overview
    assert "Sprint qualifying" not in overview  # no sprint qualifying data: no tab
    html = _get(client, f"/events/{mexico.id}?tab=qualifying")
    assert "Out in Q1" in html and "Out in Q2" in html and "1:17.000" in html
    assert 'class="num mono q-best"' in html and "(fastest in Q1)" in html
    data = json.loads(re.search(r'id="quali-chart-data">(.*?)</script>', html, re.S)[1])
    assert data["labels"][:2] == ["D01", "D02"] and len(data["labels"]) == 21  # D22: no time
    assert data["followed"][1] is True
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag or 'type="application/json"' in tag, tag
    # Unknown tabs fall back to the overview.
    assert "Out in Q1" not in _get(client, f"/events/{mexico.id}?tab=nope")


def test_qualifying_tab_respects_spoiler_mode(db, client, mexico):
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    soon = QUALI_AT + timedelta(hours=1)
    html = _get(client, f"/events/{mexico.id}?tab=qualifying", soon)
    assert "Hidden by spoiler mode" in html
    assert "1:15.500" not in html and "quali-chart-data" not in html and "D01" not in html
    revealed = _get(client, f"/events/{mexico.id}?tab=qualifying&reveal=1", soon)
    assert "1:15.500" in revealed
    assert "?tab=qualifying&amp;reveal=1" in revealed  # tab links keep the reveal


def test_sprint_qualifying_tab_only_with_data(db, client, mexico):
    sq = Session(type="SQ", start_utc=QUALI_AT - timedelta(days=1))
    mexico.sessions.append(sq)
    db.commit()
    assert "Sprint qualifying" not in _get(client, f"/events/{mexico.id}")
    db.add(Result(session_id=sq.id, driver_id="driver01", position=1, q1="1:17.1", q2="1:16.9"))
    db.add(Result(session_id=sq.id, driver_id="driver02", position=2, q1="1:17.2"))
    db.commit()
    html = _get(client, f"/events/{mexico.id}?tab=sprint-qualifying")
    assert "Sprint qualifying" in html and "Out in SQ1" in html and "SQ2" in html


def test_driver_page_qualifying_stats(db, client, mexico):
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=RACE_AT)
    for race in races("qualifying_2025_3.json"):
        apply_results(db, race, "Q", now=QUALI_AT)  # leclerc: P2, reached Q3
    db.commit()
    q = qualifying.driver_season(db, "leclerc", 2025)
    assert (q.sessions, q.avg_position, q.q3) == (1, 2.0, 1)
    html = _get(client, "/drivers/leclerc?season=2025")
    assert "Q3 appearances" in html and "1 of 1" in html
    rounds = json.loads(re.search(r'id="quali-pos-data">(.*?)</script>', html, re.S)[1])
    assert rounds == [
        {
            "round": 3,
            "label": "MEX",
            "name": "Mexico City Grand Prix",
            "event_id": mexico.id,
            "position": 2,
            "reached": 3,
        }
    ]
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    held = _get(client, "/drivers/leclerc?season=2025", RACE_AT + timedelta(hours=1))
    assert "Q3 appearances" not in held and "quali-pos-data" not in held
