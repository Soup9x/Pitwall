"""Dashboard → Rearrange: per-user card order, separate for the normal and race-weekend
layouts, with arrow buttons (no JavaScript needed) and drag-and-drop saving."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from pitwall.settings_store import load_settings
from pitwall.users import create_user
from pitwall.web import auth
from pitwall.web.dashboard import DEFAULT_LAYOUT, layout, move, reorder
from tests.test_phase2 import season  # noqa: F401 (fixture: a fictional 2025 season)


def test_layout_keeps_known_cards_and_adds_new_ones():
    assert layout(None, "normal") == DEFAULT_LAYOUT["normal"]
    saved = {"normal": ["news", "bogus", "news", "drivers"]}
    order = layout(saved, "normal")
    assert order[:2] == ["news", "drivers"]
    assert sorted(order) == sorted(DEFAULT_LAYOUT["normal"])  # nothing lost or duplicated
    assert layout(saved, "weekend") == DEFAULT_LAYOUT["weekend"]  # layouts are separate


def test_reorder_and_move_skip_cards_that_are_not_on_screen():
    order = ["hero", "drivers", "followed", "constructors", "news", "timeline"]
    on_screen = ["hero", "drivers", "constructors", "news"]  # no followed drivers, no event
    # "Down" from drivers swaps with constructors: the hidden "followed" card isn't a step.
    assert move(order, on_screen, "drivers", "down") == [
        "hero",
        "constructors",
        "followed",
        "drivers",
        "news",
        "timeline",
    ]
    assert move(order, on_screen, "hero", "up") == order  # already first
    assert reorder(order, ["news", "hero", "drivers", "constructors"]) == [
        "news",
        "hero",
        "followed",
        "drivers",
        "constructors",
        "timeline",
    ]


@pytest.fixture
def client(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _login(c, username="admin", password="correct horse battery"):
    c.cookies.clear()
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
    c.post("/login", data={"username": username, "password": password, "csrf_token": token})
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/settings").text)[1]


def _cards(html):
    return re.findall(r'data-card="([a-z]+)"', html)


def test_rearranging_via_the_dashboard(client, db, season):  # noqa: F811
    db.commit()
    csrf = _login(client)
    page = client.get("/").text
    assert "Rearrange cards" in page and "card-tools" not in page and "dashboard.js" not in page
    assert _cards(page)[:2] == ["hero", "drivers"]
    editing = client.get("/?edit=1").text
    assert 'class="card-tools"' in editing and '<script src="/static/dashboard.js">' in editing
    assert 'draggable="true"' in editing
    visible = ",".join(_cards(editing))
    r = client.post(
        "/prefs/dashboard",
        data={
            "csrf_token": csrf,
            "mode": "normal",
            "card": "drivers",
            "direction": "up",
            "visible": visible,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/?edit=1#card-drivers"
    assert _cards(client.get("/").text)[:2] == ["drivers", "hero"]
    # Drag and drop sends the whole order.
    r = client.post(
        "/prefs/dashboard",
        data={"csrf_token": csrf, "mode": "normal", "order": "news,constructors,drivers,hero"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 204
    assert _cards(client.get("/").text)[:4] == ["news", "constructors", "drivers", "hero"]
    assert load_settings(db, 1).dashboard_layout["normal"][0] == "news"
    client.post("/prefs/dashboard", data={"csrf_token": csrf, "mode": "normal", "reset": "1"})
    assert _cards(client.get("/").text)[:2] == ["hero", "drivers"]
    bad = client.post("/prefs/dashboard", data={"csrf_token": csrf, "mode": "sideways"})
    assert bad.status_code == 400


def test_each_user_has_their_own_layout(client, db, season):  # noqa: F811
    create_user(db, "friend", "a long password 1")
    db.commit()
    csrf = _login(client, "friend", "a long password 1")
    client.post(
        "/prefs/dashboard",
        data={"csrf_token": csrf, "mode": "normal", "order": "news,hero,drivers"},
    )
    assert _cards(client.get("/").text)[0] == "news"
    _login(client)
    assert _cards(client.get("/").text)[0] == "hero"  # the admin's dashboard is unchanged
