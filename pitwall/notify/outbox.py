"""Alert queue: dedup, routing, quiet hours and spoiler holds.

Every alert is enqueued here. `flush()` (run every minute by the scheduler, and right after
enqueueing) decides for each pending alert whether to send, hold or drop it:

* expired (e.g. a reminder whose session already started) -> dropped
* results/standings while spoiler mode is on and the event isn't marked watched -> held
* a type folded into the weekly digest (when digest mode is on) -> held for the digest
* inside quiet hours -> held until the window ends
* otherwise -> delivered to each routed channel, once per channel (never twice)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import NotificationLog, Outbox, SpoilerState
from pitwall.notify.dispatch import notify
from pitwall.notify.message import Message
from pitwall.settings_store import SPOILER_TYPES, AppSettings, load_settings
from pitwall.timeutil import in_window

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
Sender = Callable[[str, Message], tuple[bool, str]]


def already_sent(db: DB, payload_hash: str, channel: str | None = None) -> bool:
    q = select(NotificationLog.id).where(
        NotificationLog.payload_hash == payload_hash, NotificationLog.status == "sent"
    )
    if channel:
        q = q.where(NotificationLog.channel == channel)
    return db.scalars(q.limit(1)).first() is not None


def enqueue(
    db: DB, msg: Message, *, settings: AppSettings | None = None, now: datetime | None = None
) -> Outbox | None:
    """Queue an alert. Returns None if it is a duplicate or nothing is routed for its type."""
    settings = settings or load_settings(db)
    now = now or utcnow()
    h = msg.payload_hash()
    dup = db.scalars(select(Outbox.id).where(Outbox.payload_hash == h).limit(1)).first()
    if dup is not None or already_sent(db, h):
        log.info("duplicate alert suppressed", extra={"type": msg.type, "hash": h[:12]})
        return None
    channels = settings.channels_for(msg.type)
    if not channels:
        log.info("alert type not routed to any channel", extra={"type": msg.type})
        return None
    row = Outbox(
        type=msg.type,
        payload_hash=h,
        event_id=msg.event_id,
        session_id=msg.session_id,
        title=msg.title,
        text=msg.text,
        html=msg.html,
        data_json=json.dumps({"data": msg.data, "url": msg.url}),
        channels=",".join(channels),
        created_at=now,
        expires_at=msg.expires_at,
        spoiler=msg.type in SPOILER_TYPES,
        status="pending",
    )
    db.add(row)
    db.flush()
    log.info("alert queued", extra={"type": msg.type, "outbox_id": row.id, "channels": channels})
    return row


def _row_message(row: Outbox) -> Message:
    extra = json.loads(row.data_json or "{}")
    return Message(
        type=row.type,
        title=row.title,
        text=row.text,
        html=row.html,
        data=extra.get("data") or {},
        event_id=row.event_id,
        session_id=row.session_id,
        url=extra.get("url"),
    )


def spoiler_released(db: DB, row: Outbox, settings: AppSettings, now: datetime) -> bool:
    if not settings.spoiler_enabled or not row.spoiler or row.event_id is None:
        return True
    state = db.get(SpoilerState, row.event_id)
    if state is not None and state.watched:
        return True
    hours = settings.spoiler_auto_release_hours
    return bool(hours and hours > 0 and now >= row.created_at + timedelta(hours=hours))


def in_quiet_hours(settings: AppSettings, now: datetime) -> bool:
    window = settings.quiet_window
    if window is None:
        return False
    return in_window(now.astimezone(settings.tz).time(), *window)


def _log(db: DB, row: Outbox, channel: str, status: str, detail: str, now: datetime) -> None:
    db.add(
        NotificationLog(
            type=row.type,
            channel=channel,
            payload_hash=row.payload_hash,
            title=row.title,
            event_id=row.event_id,
            session_id=row.session_id,
            sent_at=now,
            status=status,
            detail=detail[:1000],
        )
    )


def flush(db: DB, *, now: datetime | None = None, sender: Sender | None = None) -> dict[str, int]:
    now = now or utcnow()
    sender = sender or notify
    settings = load_settings(db)
    quiet = in_quiet_hours(settings, now)
    stats = {"sent": 0, "held": 0, "dropped": 0, "failed": 0}

    pending = db.scalars(
        select(Outbox).where(Outbox.status == "pending").order_by(Outbox.created_at, Outbox.id)
    ).all()
    for row in pending:
        if row.expires_at is not None and now >= row.expires_at:
            row.status, row.hold_reason, row.resolved_at = "dropped", None, now
            _log(db, row, "-", "dropped", "expired before it could be delivered", now)
            stats["dropped"] += 1
            continue
        if not spoiler_released(db, row, settings, now):
            row.hold_reason = "spoiler"
            stats["held"] += 1
            continue
        if settings.digest_enabled and row.type in settings.digest_types:
            row.hold_reason = "digest"  # collected by the weekly digest job
            stats["held"] += 1
            continue
        if quiet:
            row.hold_reason = "quiet"
            stats["held"] += 1
            continue

        msg = _row_message(row)
        all_ok = True
        for channel in [c for c in row.channels.split(",") if c]:
            if already_sent(db, row.payload_hash, channel):
                continue
            try:
                ok, detail = sender(channel, msg)
            except Exception as exc:  # never let one bad channel break the queue
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            _log(db, row, channel, "sent" if ok else "failed", detail, now)
            all_ok &= ok
        row.attempts += 1
        row.hold_reason = None
        if all_ok:
            row.status, row.resolved_at = "sent", now
            stats["sent"] += 1
        elif row.attempts >= MAX_ATTEMPTS:
            row.status, row.resolved_at = "failed", now
            stats["failed"] += 1
            log.warning(
                "alert delivery failed permanently", extra={"type": row.type, "outbox_id": row.id}
            )
        else:
            stats["failed"] += 1
    db.flush()
    return stats


def mark_watched(
    db: DB, event_id: int, watched: bool = True, *, now: datetime | None = None
) -> None:
    state = db.get(SpoilerState, event_id)
    if state is None:
        state = SpoilerState(event_id=event_id)
        db.add(state)
    state.watched = watched
    state.watched_at = (now or utcnow()) if watched else None
    db.flush()


def is_watched(db: DB, event_id: int) -> bool:
    state = db.get(SpoilerState, event_id)
    return bool(state and state.watched)
