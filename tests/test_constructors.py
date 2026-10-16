"""Constructor pages: a team's season, its drivers, race by race and season history, linked
from everywhere a team name appears, and hidden while spoiler mode holds the season."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from pitwall.services import constructor_stats
from pitwall.settings_store import save_settings
from pitwall.web import auth
from tests.test_phase2 import season  # noqa: F401 (fixture: a fictional 2025 season)


def test_team_season(db, season):  # noqa: F811
    data = constructor_stats.season(db, "redline", 2025)
    assert [r.event.round for r in data.rounds] == [1, 2, 3]
    assert [r.points for r in data.rounds] == [40, 48, 0]  # round 2 includes the sprint
    assert data.points == 88 and data.standing_points == 88 and data.standing_position == 1
    assert (data.wins, data.podiums, data.one_twos, data.double_points) == (2, 3, 0, 2)
    assert (data.poles, data.front_rows, data.quali_sessions) == (3, 3, 3)
    assert (data.dnfs, data.starts) == (2, 6)
    assert [(d.driver.id, d.points) for d in data.drivers] == [
        ("bob_bolt", 45),
        ("alice_ace", 43),
    ]
    bob = data.drivers[0]
    assert (bob.races, bob.wins, bob.best, bob.cumulative) == (3, 1, 1, [15, 45, 45])
    assert bob.avg_quali == pytest.approx(1.3, abs=0.05)
    assert constructor_stats.season(db, "nobody", 2025) is None


def test_history(db, season):  # noqa: F811
    rows = constructor_stats.history(db, "redline")
    assert [(h.season, h.races, h.wins, h.podiums, h.points, h.position) for h in rows] == [
        (2025, 3, 2, 3, 88, 1)
    ]
    assert constructor_stats.history(db, "redline", upto_season=2024) == []


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
        c.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        yield c


def test_page_and_links(client, db, season):  # noqa: F811
    db.commit()
    page = client.get("/constructors/redline?season=2025").text
    assert "Redline" in page and "P1 in the 2025 constructors" in page
    assert 'href="/drivers/bob_bolt?season=2025"' in page  # drivers link to their pages
    assert "Race by race" in page and "<polyline" in page
    assert client.get("/constructors/nobody").status_code == 404
    standings = client.get("/standings?season=2025").text
    assert 'href="/constructors/redline?season=2025"' in standings
    driver = client.get("/drivers/alice_ace?season=2025").text
    assert 'href="/constructors/redline?season=2025"' in driver


def test_held_season_is_hidden(client, db, season):  # noqa: F811
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    page = client.get("/constructors/redline?season=2025").text
    assert "Hidden by spoiler mode" in page
    assert "Race by race" not in page and "Bob Bolt" not in page and ">88<" not in page
    assert "Race by race" in client.get("/constructors/redline?season=2025&reveal=1").text


def test_dashboard_constructors_card(client, db, season):  # noqa: F811
    from pitwall.models import FollowedDriver

    db.add(FollowedDriver(driver_id="cy_crash"))
    db.commit()
    page = client.get("/").text
    assert 'aria-label="Top 10 constructors"' in page
    assert 'href="/constructors/redline?season=2025">Redline</a>' in page
    assert "(a followed driver races here)" in page  # Azure: Cy Crash's team
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    assert 'aria-label="Top 10 constructors"' not in client.get("/").text
