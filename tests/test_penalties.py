"""Penalty watch: manual entries win, documents are parsed defensively, alerts and spoilers.

The FIA-style documents here are short, hand-written texts in the layout of stewards'
decisions and "power unit elements used" documents; they are not copies of real documents."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from pitwall.models import Event, FiaDocument, GridPenalty, LicencePoints, Outbox, PuUsage
from pitwall.services import fia_docs, penalties, penalty_watch
from pitwall.services.sync import apply_results, apply_schedule
from pitwall.settings_store import save_settings, set_kv
from pitwall.sources.fia import DocRef, parse_listing
from pitwall.web import auth
from tests.conftest import dt, races
from tests.pdfgen import make_pdf


@pytest.fixture
def season(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    for race in races("results_2025_3.json"):
        apply_results(db, race, "RACE", now=dt("2025-10-26T22:00:00"))
    db.commit()
    return {e.slug: e for e in db.scalars(select(Event))}


def decision(car="1", name="Max Verstappen", body="5 place grid penalty for this Competition."):
    return make_pdf(
        [
            "From The Stewards",
            "Document 31",
            f"No / Driver {car} - {name}",
            "Session Qualifying",
            "Fact Impeding Car 4 at Turn 12.",
            f"Decision {body}",
            "Reason The Stewards heard from the driver and team representative.",
        ]
    )


# ---------------------------------------------------------------------------- parsing


def test_decision_parser():
    d = fia_docs.parse_decision(fia_docs.extract_text(decision()))
    assert (d.car, d.grid_places, d.back_of_grid, d.next_event) == (1, 5, False, False)
    assert d.reason == "Impeding Car 4 at Turn 12."
    d = fia_docs.parse_decision(
        "No / Driver 16 - Charles Leclerc\nDecision Drop of 3 grid positions at the driver's "
        "next race. 2 penalty points imposed (total of 6 points in the 12 month period).\n"
        "Reason x"
    )
    assert (d.grid_places, d.next_event, d.points, d.points_total) == (3, True, 2, 6)
    d = fia_docs.parse_decision(
        "No / Driver 44 - Lewis Hamilton\nDecision Required to start the race from the back "
        "of the grid.\nReason x"
    )
    assert d.back_of_grid and d.grid_places is None
    # Nothing tracked: a reprimand isn't a penalty here.
    assert fia_docs.parse_decision("No / Driver 4 - Lando Norris\nDecision Reprimand.") is None


@pytest.mark.parametrize(
    "text",
    [
        "Decision 5 place grid penalty.",  # no driver
        "No / Driver 4 - Lando Norris\nDecision 5 place grid penalty and a 10 place grid penalty",
        "No / Driver 4 - Lando Norris\nDecision 99 place grid penalty.",
        "No / Driver 4 - Lando Norris\nDecision 15 penalty points imposed.",
    ],
)
def test_decision_parser_refuses_odd_documents(text):
    with pytest.raises(fia_docs.ParseError):
        fia_docs.parse_decision(text)


USAGE = [
    "Power Unit Elements Used",
    "Car Driver ICE TC MGU-H MGU-K ES CE EX",
    "1 Max VERSTAPPEN 3 3 3 3 2 2 5",
    "4 Lando NORRIS 2 2 2 2 1 1 4",
]


def test_pu_usage_parser():
    table = fia_docs.parse_pu_usage("\n".join(USAGE))
    assert table[1]["ICE"] == 3 and table[4]["EX"] == 4 and table[4]["MGU-H"] == 2
    with pytest.raises(fia_docs.ParseError):
        fia_docs.parse_pu_usage("\n".join([*USAGE[:2], "1 Max VERSTAPPEN 3 3 3"]))
    with pytest.raises(fia_docs.ParseError):
        fia_docs.parse_pu_usage("Nothing useful here")


def test_listing_parser():
    page = (
        '<div><a href="/sites/default/files/decision-document/2025%20Mexico%20City%20Grand'
        '%20Prix%20-%20Decision%20-%20Car%201.pdf"><div class="title">Doc 31 - Decision - Car 1'
        '</div></a><div class="published">Published on 25.10.25 23:40 CET</div></div>'
    )
    (ref,) = parse_listing(page, "https://www.fia.com/documents/x")
    assert ref.url.startswith("https://www.fia.com/sites/default/files/decision-document/")
    assert ref.title == "Doc 31 - Decision - Car 1"
    assert ref.published_at == datetime(2025, 10, 25, 22, 40, tzinfo=UTC)


# ---------------------------------------------------------------------------- storing


def test_ingest_decision_and_points(db, season):
    mexico = season["2025-rodriguez"]
    res = fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/31.pdf",
        title="Doc 31 - Decision - Car 1",
        pdf=decision(body="5 place grid penalty. 2 penalty points imposed (total 2 points)."),
        published_at=dt("2025-10-25T22:40:00"),
    )
    assert res.document.status == "parsed"
    (g,) = penalties.grid_penalties(db, mexico)
    assert (g.driver_id, g.places, g.document.url) == ("max_verstappen", 5, res.document.url)
    assert penalties.driver_total(db, "max_verstappen", date(2025, 11, 1)) == 2


def test_wrong_name_or_unknown_car_stores_nothing(db, season):
    mexico = season["2025-rodriguez"]
    for car, name in (("1", "Lando Norris"), ("99", "Nobody Atall")):
        res = fia_docs.ingest(
            db,
            mexico,
            url=f"https://www.fia.com/d/{car}.pdf",
            title="Doc - Decision",
            pdf=decision(car=car, name=name),
        )
        assert res.document.status == "failed" and not res.grid
    assert db.scalars(select(GridPenalty)).first() is None


def test_usage_going_down_is_rejected_whole(db, season):
    aus, mexico = season["2025-albert_park"], season["2025-rodriguez"]
    penalties.set_manual_usage(db, aus, "norris", "ICE", 3, user_id=1)
    res = fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/pu.pdf",
        title="Power Unit Elements Used",
        pdf=make_pdf(USAGE),  # Norris back to 2 ICEs: impossible
    )
    assert res.document.status == "failed" and "went down" in res.document.detail
    assert not db.scalars(select(PuUsage).where(PuUsage.origin == "fia")).all()

    penalties.set_manual_usage(db, aus, "norris", "ICE", 1, user_id=1)
    res = fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/pu2.pdf",
        title="Power Unit Elements Used",
        pdf=make_pdf(USAGE),
    )
    assert res.document.status == "parsed" and len(res.usage) == 14


def test_manual_values_win(db, season):
    mexico = season["2025-rodriguez"]
    fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/31.pdf",
        title="Doc 31 - Decision - Car 1",
        pdf=decision(),
    )
    penalties.add_manual_grid_penalty(db, mexico, "max_verstappen", 0, False, "overruled", 1)
    assert penalties.grid_penalties(db, mexico) == []  # manual "no penalty" beats the parse

    penalties.set_manual_usage(db, mexico, "norris", "ICE", 3, user_id=1)
    db.add(PuUsage(event_id=mexico.id, driver_id="norris", element="ICE", count=2, origin="fia"))
    db.flush()
    _, rows = penalties.pu_table(db, 2025)
    assert rows[0].cells["ICE"].count == 3 and rows[0].cells["ICE"].origin == "manual"


def test_allocation_states(db, season):
    mexico = season["2025-rodriguez"]
    penalties.set_allocation(db, 2025, "ice", 4)
    penalties.set_manual_usage(db, mexico, "norris", "ICE", 4, 1)
    penalties.set_manual_usage(db, mexico, "piastri", "ICE", 5, 1)
    penalties.set_manual_usage(db, mexico, "leclerc", "EX", 2, 1)  # no allocation entered
    _, rows = penalties.pu_table(db, 2025)
    by = {r.driver.id: r for r in rows}
    assert by["norris"].cells["ICE"].state == "limit"
    assert by["piastri"].cells["ICE"].state == "over"
    assert by["leclerc"].cells["EX"].allowed is None


def test_licence_points_roll_off_after_twelve_months(db, season):
    penalties.add_manual_points(db, "norris", 4, date(2024, 11, 3), "a", None, 1)
    penalties.add_manual_points(db, "norris", 3, date(2025, 6, 1), "b", None, 1)
    assert penalties.driver_total(db, "norris", date(2025, 11, 2)) == 7
    assert penalties.driver_total(db, "norris", date(2025, 11, 3)) == 3  # the 4 expired
    (row,) = penalties.licence_table(db, date(2025, 11, 3))
    assert row.entries[0].expires == date(2026, 6, 1)
    assert penalties.expires_on(date(2024, 2, 29)) == date(2025, 3, 1)


# ---------------------------------------------------------------------------- poll + alerts


class FakeFia:
    def __init__(self, docs: dict[str, tuple[str, bytes, datetime | None]]):
        self.docs = docs
        self.pdf_calls: list[str] = []

    def listing(self, _url):
        return [DocRef(t, u, p) for u, (t, _b, p) in self.docs.items()]

    def pdf(self, url):
        self.pdf_calls.append(url)
        return self.docs[url][1]

    def close(self):
        pass


def test_race_week_schedule(db, season):
    friday = datetime(2025, 10, 24, 18, tzinfo=UTC)  # Mexico City race week
    assert penalty_watch.race_week_event(db, friday).slug == "2025-rodriguez"
    assert penalty_watch.interval_for(season["2025-rodriguez"], friday) == timedelta(minutes=30)
    thursday = datetime(2025, 10, 23, 18, tzinfo=UTC)
    assert penalty_watch.interval_for(season["2025-rodriguez"], thursday) == timedelta(hours=2)
    assert penalty_watch.race_week_event(db, datetime(2025, 10, 20, 18, tzinfo=UTC)) is None
    set_kv(db, penalty_watch.LAST_POLL_KEY, (friday - timedelta(minutes=10)).isoformat())
    assert penalty_watch.due(db, friday) is None
    assert penalty_watch.due(db, friday + timedelta(minutes=25)) is not None


def test_poll_stores_and_alerts(db, season, sent):
    from pitwall import jobs

    save_settings(db, {"routing": {"penalty": ["webhook"], "licence": ["webhook"]}})
    penalties.add_manual_points(db, "max_verstappen", 8, date(2025, 6, 1), "earlier", None, 1)
    set_kv(db, penalty_watch.URL_KEY, "https://www.fia.com/documents/season-2025")
    db.commit()
    base = "https://www.fia.com/sites/default/files/decision-document/"
    fake = FakeFia(
        {
            base + "2025 Mexico City Grand Prix - Decision - Car 1.pdf": (
                "Doc 31 - Decision - Car 1",
                decision(body="5 place grid penalty. 2 penalty points imposed (total 10 points)."),
                dt("2025-10-24T20:00:00"),  # Friday: before qualifying
            ),
            base + "2025 Australian Grand Prix - Decision.pdf": ("Other event", b"", None),
        }
    )
    now = datetime(2025, 10, 24, 21, tzinfo=UTC)
    changes = penalty_watch.poll(fake, now)
    assert len(changes.grid) == 1 and changes.licence == [("max_verstappen", 8, 10)]
    assert len(fake.pdf_calls) == 1  # the other event's document isn't fetched
    queued = penalty_watch.send_alerts(changes, jobs.fan_out, now)
    assert queued == 2
    db.expire_all()
    titles = sorted(r.title for r in db.scalars(select(Outbox)))
    assert titles[0].startswith("⚠️ Grid penalty: Max Verstappen (5-place grid penalty)")
    assert "now has 10 licence points" in titles[1]
    assert not any(r.spoiler for r in db.scalars(select(Outbox)))  # published before quali
    # The same documents aren't fetched twice.
    set_kv(db, penalty_watch.LAST_POLL_KEY, None)
    db.commit()
    assert not penalty_watch.poll(fake, now + timedelta(hours=1))
    assert len(fake.pdf_calls) == 1


def test_post_qualifying_decisions_are_spoilers(db, season, sent):
    from pitwall import jobs

    save_settings(db, {"spoiler_enabled": True, "routing": {"penalty": ["webhook"]}})
    set_kv(db, penalty_watch.URL_KEY, "https://www.fia.com/documents/season-2025")
    db.commit()
    url = "https://www.fia.com/x/2025 Mexico City Grand Prix - Decision - Car 1.pdf"
    fake = FakeFia({url: ("Doc 40 - Decision", decision(), dt("2025-10-25T22:40:00"))})
    now = datetime(2025, 10, 25, 23, tzinfo=UTC)  # Saturday, after qualifying
    changes = penalty_watch.poll(fake, now)
    penalty_watch.send_alerts(changes, jobs.fan_out, now)
    db.expire_all()
    (row,) = db.scalars(select(Outbox))
    assert row.spoiler and row.event_id == season["2025-rodriguez"].id


# ---------------------------------------------------------------------------- pages


@pytest.fixture
def admin(engine):
    from pitwall.web.app import create_app

    auth.limiter._failures.clear()
    with TestClient(create_app(manage_db=False, start_scheduler=False)) as c:
        yield c


def _login(c, at):
    c.cookies.clear()
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)[1]
    c.post(
        "/login",
        data={"username": "admin", "password": "correct horse battery", "csrf_token": token},
    )
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/penalties").text)[1]


def test_admin_forms_and_penalty_page(admin, db, season):
    mexico = season["2025-rodriguez"]
    with time_machine.travel(datetime(2025, 10, 30, tzinfo=UTC), tick=False):
        token = _login(admin, None)
        admin.post(
            "/admin/penalties/allocation",
            data={"csrf_token": token, "season": 2025, "element": "ICE", "allowed": 4},
        )
        admin.post(
            "/admin/penalties/usage",
            data={
                "csrf_token": token,
                "season": 2025,
                "event_id": mexico.id,
                "driver_id": "piastri",
                "element": "ice",
                "count": 5,
            },
        )
        admin.post(
            "/admin/penalties/grid",
            data={
                "csrf_token": token,
                "season": 2025,
                "event_id": mexico.id,
                "driver_id": "piastri",
                "places": 10,
                "reason": "Fifth ICE",
            },
        )
        r = admin.post(
            "/admin/penalties/points",
            data={
                "csrf_token": token,
                "season": 2025,
                "driver_id": "norris",
                "points": 13,
                "awarded_on": "2025-10-26",
            },
            follow_redirects=True,
        )
        assert "Not saved" in r.text  # 13 points isn't possible
        admin.post(
            "/admin/penalties/points",
            data={
                "csrf_token": token,
                "season": 2025,
                "driver_id": "norris",
                "points": 3,
                "awarded_on": "2025-10-26",
            },
        )
        page = admin.get(f"/events/{mexico.id}").text
        assert admin.get("/penalties?season=2025").status_code == 404  # the page was removed
    assert "10-place grid penalty" in page and "Fifth ICE" in page
    assert "Edit penalties (admin)" in page
    db.expire_all()
    assert db.scalars(select(LicencePoints)).one().points == 3
    assert db.scalars(select(PuUsage)).one().count == 5


def test_unparsed_documents_link_to_source(admin, db, season):
    mexico = season["2025-rodriguez"]
    fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/broken.pdf",
        title="Doc 50 - Decision",
        pdf=b"not a pdf",
    )
    db.commit()
    with time_machine.travel(datetime(2025, 10, 30, tzinfo=UTC), tick=False):
        _login(admin, None)
        page = admin.get("/admin/penalties").text
    assert "Couldn&#39;t parse" in page or "Couldn't parse" in page
    assert 'href="https://www.fia.com/d/broken.pdf"' in page
    assert db.scalars(select(FiaDocument)).one().status == "failed"


def test_event_penalties_respect_spoilers(admin, db, season):
    mexico = season["2025-rodriguez"]
    fia_docs.ingest(
        db,
        mexico,
        url="https://www.fia.com/d/40.pdf",
        title="Doc 40 - Decision",
        pdf=decision(),
        published_at=dt("2025-10-25T22:40:00"),
    )
    save_settings(db, {"spoiler_enabled": True})
    db.commit()
    with time_machine.travel(datetime(2025, 10, 27, tzinfo=UTC), tick=False):
        _login(admin, None)
        shown = admin.get(f"/events/{mexico.id}?reveal=1").text
        event_page = admin.get(f"/events/{mexico.id}").text
    assert "5-place grid penalty" in shown
    assert "5-place grid penalty" not in event_page
