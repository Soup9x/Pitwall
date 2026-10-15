"""Tyre nominations: manual form first, parsed press releases second, nothing guessed."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event
from pitwall.services import tyres
from pitwall.services.sync import apply_schedule
from pitwall.web import auth
from tests.conftest import dt, races


@pytest.fixture
def events(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    apply_schedule(db, 2024, races("schedule_2024.json"), now=dt("2024-03-01T00:00:00"), curated={})
    db.commit()
    by_slug = {e.slug: e for e in db.scalars(select(Event))}
    return by_slug


@pytest.fixture
def admin(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
        c.post(
            "/login",
            data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
        )
        yield c


def _csrf(c, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text)[1]


def test_validate_orders_compounds_hard_to_soft():
    assert tyres.validate("c2", "C3", "c4") == ("C2", "C3", "C4")
    for bad in (("C3", "C2", "C4"), ("C2", "C2", "C3"), ("C0", "C2", "C3"), ("", "C2", "C3")):
        with pytest.raises(tyres.InvalidNomination):
            tyres.validate(*bad)


def test_manual_form_and_not_announced(admin, db, events):
    mexico = events["2025-rodriguez"]
    page = admin.get(f"/events/{mexico.id}").text
    assert "Not announced yet" in page
    r = admin.post(
        f"/admin/events/{mexico.id}/tyres",
        data={
            "csrf_token": _csrf(admin, f"/events/{mexico.id}"),
            "hard": "C2",
            "medium": "C3",
            "soft": "C4",
        },
        follow_redirects=True,
    )
    assert "Tyre nomination saved" in r.text
    assert 'title="Hard: C2"' in r.text and "Entered by an admin" in r.text
    # An impossible order is refused and nothing changes.
    r = admin.post(
        f"/admin/events/{mexico.id}/tyres",
        data={
            "csrf_token": _csrf(admin, f"/events/{mexico.id}"),
            "hard": "C5",
            "medium": "C3",
            "soft": "C4",
        },
        follow_redirects=True,
    )
    assert "Tyres not saved" in r.text
    db.expire_all()
    assert tyres.effective(db, mexico.id).hard == "C2"


def test_manual_beats_parsed_and_last_year_is_shown(admin, db, events):
    mexico_24, mexico_25 = events["2024-rodriguez"], events["2025-rodriguez"]
    tyres.store_parsed(
        db,
        mexico_24.id,
        ("C3", "C4", "C5"),
        title="2024 Mexico City GP preview",
        url="https://press.pirelli.com/example",
        published_at=dt("2024-10-21T10:00:00"),
    )
    tyres.store_parsed(
        db,
        mexico_25.id,
        ("C3", "C4", "C5"),
        title="preview",
        url="https://press.pirelli.com/x",
        published_at=None,
    )
    tyres.set_manual(db, mexico_25.id, "C2", "C3", "C4", user_id=1)
    db.commit()
    assert tyres.effective(db, mexico_25.id).origin == "manual"
    view = tyres.view(db, mexico_25)
    assert view.last_year[0].season == 2024 and view.last_year[1].soft == "C5"
    page = admin.get(f"/events/{mexico_25.id}").text
    assert "2024 here" in page


def test_non_admins_cannot_edit(engine, db, events):
    from pitwall import users
    from pitwall.web.app import create_app

    users.create_user(db, "bob", "correct-staple-42x")
    db.commit()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
        c.post(
            "/login",
            data={"username": "bob", "password": "correct-staple-42x", "csrf_token": token},
        )
        mexico = events["2025-rodriguez"]
        assert "Admin: enter" not in c.get(f"/events/{mexico.id}").text
        r = c.post(
            f"/admin/events/{mexico.id}/tyres",
            data={"csrf_token": _csrf(c, "/"), "hard": "C1", "medium": "C2", "soft": "C3"},
        )
        assert r.status_code == 403
    assert tyres.effective(db, mexico.id) is None


# Parser tests use short, hand-written sentences in the styles Pirelli previews use; they are
# not quotes from real releases (see tests/fixtures/pirelli/README.md for recorded ones).
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Pirelli has chosen C2 as the P Zero White hard, C3 as the P Zero Yellow medium "
            "and C4 as the P Zero Red soft.",
            ("C2", "C3", "C4"),
        ),
        ("The nomination is the hard C1, the medium C2 and the soft C3.", ("C1", "C2", "C3")),
        ("Compounds: C4 (hard), C5 (medium), C6 (soft).", ("C4", "C5", "C6")),
    ],
)
def test_press_release_parser(text, expected):
    assert tyres.parse_press_release(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "A preview with no compounds in it at all.",
        # Two different hards (this year's and last year's): ambiguous, so no answer.
        "C2 as the hard, C3 as the medium, C4 as the soft. Last year C1 was the hard.",
        # Out of order: rejected rather than stored.
        "C4 as the hard, C3 as the medium and C5 as the soft.",
    ],
)
def test_press_release_parser_refuses_to_guess(text):
    assert tyres.parse_press_release(text) is None


def test_import_from_press_release_link(db, events):
    import httpx

    mexico = events["2025-rodriguez"]
    page = (
        "<html><head><title>2025 Mexico City GP preview</title><script>var C9=1;</script>"
        "</head><body><p>For Mexico, Pirelli brings the C3 as the P Zero White hard, the C4 as "
        "the medium and the C5 as the soft.</p></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    row = tyres.import_from_url(db, mexico.id, "https://press.pirelli.com/2025-mexico", client)
    assert (row.hard, row.medium, row.soft, row.origin) == ("C3", "C4", "C5", "pirelli")
    assert row.source_title == "2025 Mexico City GP preview"
    with pytest.raises(tyres.InvalidNomination):
        tyres.import_from_url(db, mexico.id, "https://example.com/not-pirelli", client)
    vague = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<p>Soft tyres!</p>"))
    )
    with pytest.raises(tyres.InvalidNomination):
        tyres.import_from_url(db, mexico.id, "https://press.pirelli.com/x", vague)
