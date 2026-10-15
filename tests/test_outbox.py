"""Dedup, routing, quiet hours and spoiler holds."""

from __future__ import annotations

from datetime import time, timedelta

from sqlalchemy import select

from pitwall.models import Event, NotificationLog, Outbox
from pitwall.notify import outbox
from pitwall.notify.message import Message
from pitwall.services.sync import apply_schedule
from pitwall.settings_store import save_settings
from pitwall.timeutil import in_window, next_window_end
from tests.conftest import dt, races

NOW = dt("2025-10-26T16:00:00")  # 12:00 in New York


def _msg(**kw):
    base = dict(
        type="results", title="🏁 Race results — Mexico City GP", text="P1 Norris", event_id=None
    )
    base.update(kw)
    return Message(**base)


def _event(db):
    apply_schedule(db, 2025, races("schedule_2025.json"), now=dt("2025-03-01T00:00:00"), curated={})
    return db.scalars(select(Event).where(Event.slug == "2025-rodriguez")).one()


# ---- dedup


def test_same_alert_is_only_queued_and_sent_once(db, sent):
    assert outbox.enqueue(db, _msg(), now=NOW) is not None
    assert outbox.enqueue(db, _msg(), now=NOW) is None
    outbox.flush(db, now=NOW)
    outbox.flush(db, now=NOW + timedelta(minutes=1))
    assert outbox.enqueue(db, _msg(), now=NOW + timedelta(hours=1)) is None
    outbox.flush(db, now=NOW + timedelta(hours=1))
    assert sent.titles() == ["🏁 Race results — Mexico City GP"]  # results -> webhook only


def test_different_content_is_not_a_duplicate(db, sent):
    outbox.enqueue(db, _msg(), now=NOW)
    outbox.enqueue(db, _msg(text="P1 Piastri"), now=NOW)
    outbox.flush(db, now=NOW)
    assert len(sent.items) == 2


def test_routing_sends_to_each_configured_channel(db, sent):
    outbox.enqueue(db, _msg(type="schedule_change", title="change"), now=NOW)
    outbox.flush(db, now=NOW)
    assert sorted(c for c, _ in sent.items) == ["email", "webhook"]


def test_unrouted_alert_type_is_not_queued(db, sent):
    routing = {"results": [], "reminder": ["webhook"]}
    save_settings(db, {"routing": routing})
    assert outbox.enqueue(db, _msg(), now=NOW) is None


def test_partial_failure_retries_only_failed_channel(db, sent):
    sent.fail.add("email")
    outbox.enqueue(db, _msg(type="schedule_change", title="change"), now=NOW)
    outbox.flush(db, now=NOW)
    assert [c for c, _ in sent.items] == ["webhook"]
    row = db.scalars(select(Outbox)).one()
    assert row.status == "pending" and row.attempts == 1
    sent.fail.clear()
    outbox.flush(db, now=NOW + timedelta(minutes=1))
    assert sorted(c for c, _ in sent.items) == ["email", "webhook"]
    assert row.status == "sent"
    statuses = [r.status for r in db.scalars(select(NotificationLog).order_by(NotificationLog.id))]
    assert statuses == ["failed", "sent", "sent"]


def test_permanent_failure_after_max_attempts(db, sent):
    sent.fail.add("webhook")
    row = outbox.enqueue(db, _msg(), now=NOW)
    for i in range(outbox.MAX_ATTEMPTS):
        outbox.flush(db, now=NOW + timedelta(minutes=i))
    assert row.status == "failed"


# ---- quiet hours


def test_window_helpers_handle_midnight_wrap():
    assert in_window(time(23, 30), time(23, 0), time(7, 0))
    assert in_window(time(3, 0), time(23, 0), time(7, 0))
    assert not in_window(time(7, 0), time(23, 0), time(7, 0))
    assert not in_window(time(12, 0), time(23, 0), time(7, 0))
    assert in_window(time(13, 0), time(12, 0), time(14, 0))
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    end = next_window_end(dt("2025-10-27T04:00:00"), ny, time(23, 0), time(7, 0))  # 00:00 NY
    assert end == dt("2025-10-27T11:00:00")  # 07:00 EDT


def test_quiet_hours_hold_then_deliver(db, sent):
    save_settings(db, {"quiet_enabled": True, "quiet_start": "23:00", "quiet_end": "07:00"})
    night = dt("2025-10-27T05:00:00")  # 01:00 New York (EDT ended Nov 2)
    outbox.enqueue(db, _msg(), now=night)
    stats = outbox.flush(db, now=night)
    assert stats["held"] == 1 and sent.items == []
    assert db.scalars(select(Outbox)).one().hold_reason == "quiet"
    morning = dt("2025-10-27T11:00:00")  # 07:00 New York
    outbox.flush(db, now=morning)
    assert len(sent.items) == 1


def test_reminder_for_started_session_dropped_after_quiet_hours(db, sent):
    save_settings(db, {"quiet_enabled": True, "quiet_start": "00:00", "quiet_end": "08:00"})
    start = dt("2025-10-26T09:00:00")  # session at 05:00 New York
    msg = _msg(type="reminder", title="Race in 1 hour", expires_at=start)
    outbox.enqueue(db, msg, now=dt("2025-10-26T08:00:00"))
    outbox.flush(db, now=dt("2025-10-26T08:00:00"))  # 04:00 NY: held
    assert sent.items == []
    outbox.flush(db, now=dt("2025-10-26T12:00:00"))  # 08:00 NY: session already started
    assert sent.items == []
    row = db.scalars(select(Outbox)).one()
    assert row.status == "dropped"
    assert db.scalars(select(NotificationLog)).one().status == "dropped"


# ---- spoiler mode


def test_spoiler_holds_results_until_watched(db, sent):
    ev = _event(db)
    save_settings(db, {"spoiler_enabled": True})
    outbox.enqueue(db, _msg(event_id=ev.id), now=NOW)
    outbox.enqueue(db, _msg(type="standings", title="Standings", event_id=ev.id), now=NOW)
    outbox.flush(db, now=NOW)
    assert sent.items == []
    assert {r.hold_reason for r in db.scalars(select(Outbox))} == {"spoiler"}
    outbox.mark_watched(db, ev.id)
    outbox.flush(db, now=NOW + timedelta(minutes=1))
    assert sorted(sent.titles()) == ["Standings", "🏁 Race results — Mexico City GP"]


def test_spoiler_never_holds_reminders_or_previews(db, sent):
    ev = _event(db)
    save_settings(
        db, {"spoiler_enabled": True, "routing": {"reminder": ["webhook"], "preview": ["email"]}}
    )
    outbox.enqueue(db, _msg(type="reminder", title="r", event_id=ev.id), now=NOW)
    outbox.enqueue(db, _msg(type="preview", title="p", event_id=ev.id), now=NOW)
    outbox.flush(db, now=NOW)
    assert sorted(sent.titles()) == ["p", "r"]


def test_spoiler_auto_release(db, sent):
    ev = _event(db)
    save_settings(db, {"spoiler_enabled": True, "spoiler_auto_release_hours": 12})
    outbox.enqueue(db, _msg(event_id=ev.id), now=NOW)
    outbox.flush(db, now=NOW + timedelta(hours=11, minutes=59))
    assert sent.items == []
    outbox.flush(db, now=NOW + timedelta(hours=12))
    assert len(sent.items) == 1


def test_disabling_spoiler_mode_releases_held_alerts(db, sent):
    ev = _event(db)
    save_settings(db, {"spoiler_enabled": True})
    outbox.enqueue(db, _msg(event_id=ev.id), now=NOW)
    outbox.flush(db, now=NOW)
    save_settings(db, {"spoiler_enabled": False})
    outbox.flush(db, now=NOW)
    assert len(sent.items) == 1


def test_quiet_hours_still_apply_after_watched(db, sent):
    ev = _event(db)
    save_settings(
        db,
        {
            "spoiler_enabled": True,
            "quiet_enabled": True,
            "quiet_start": "22:00",
            "quiet_end": "06:00",
        },
    )
    night = dt("2025-10-27T03:00:00")  # 23:00 NY
    outbox.enqueue(db, _msg(event_id=ev.id), now=night)
    outbox.mark_watched(db, ev.id)
    outbox.flush(db, now=night)
    assert sent.items == []
    assert db.scalars(select(Outbox)).one().hold_reason == "quiet"
