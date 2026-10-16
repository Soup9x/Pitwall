"""How fresh each data section is, for the "Synced 12 min ago" badges.

States: ok, stale (older than expected: shown with ▲ in yellow) and failed (the last attempt
failed after the last success: ✕ in red). The symbol and the words always go with the colour."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.db import utcnow
from pitwall.models import NewsFeed, SourceStatus
from pitwall.settings_store import get_kv
from pitwall.timeutil import humanize_delta

# How old data may be before it counts as stale (the jobs run more often than this).
STALE_AFTER = {
    "jolpica": timedelta(hours=12),
    "openmeteo": timedelta(hours=7),
    "openf1": timedelta(hours=26),
    "news": timedelta(hours=2),
    "fia": timedelta(days=8),
}
LABELS = {
    "jolpica": "Schedule & results",
    "openmeteo": "Forecast",
    "openf1": "OpenF1",
    "news": "News",
    "fia": "FIA documents",
}


@dataclass
class Freshness:
    source: str
    state: str  # ok / stale / failed / never
    last_success: datetime | None
    last_error: str | None = None
    now: datetime | None = None

    @property
    def symbol(self) -> str:
        return {"ok": "●", "stale": "▲", "failed": "✕", "never": "○"}[self.state]

    @property
    def text(self) -> str:
        now = self.now or utcnow()
        if self.state == "failed":
            since = f"; last good {self._ago(now)}" if self.last_success else ""
            return f"Last sync failed{since}"
        if self.last_success is None:
            return "Not synced yet"
        return f"Synced {self._ago(now)}"

    def _ago(self, now: datetime) -> str:
        assert self.last_success is not None
        secs = (now - self.last_success).total_seconds()
        return "just now" if secs < 60 else f"{humanize_delta(secs)} ago"

    @property
    def css(self) -> str:
        return {"ok": "", "stale": "stale", "failed": "failed", "never": "stale"}[self.state]


def _state(
    success: datetime | None, error_at: datetime | None, stale_after: timedelta, now: datetime
) -> str:
    if error_at is not None and (success is None or error_at > success):
        return "failed"
    if success is None:
        return "never"
    return "stale" if now - success > stale_after else "ok"


def of(db: DB, source: str, now: datetime | None = None) -> Freshness:
    now = now or utcnow()
    stale = STALE_AFTER.get(source, timedelta(hours=12))
    if source == "news":
        feeds = list(db.scalars(select(NewsFeed).where(NewsFeed.enabled)))
        ok = [f.last_success_at for f in feeds if f.last_success_at]
        errors = [f.last_error_at for f in feeds if f.last_error_at and f.last_error]
        success = max(ok) if ok else None
        # Failed only when every feed is failing; one good feed keeps the strip fresh.
        error_at = max(errors) if feeds and len(errors) == len(feeds) else None
        return Freshness(source, _state(success, error_at, stale, now), success, None, now)
    if source == "fia":
        last = get_kv(db, "fia_last_poll")
        try:
            success = datetime.fromisoformat(last) if last else None
        except ValueError:
            success = None
        return Freshness(source, _state(success, None, stale, now), success, None, now)
    row = db.get(SourceStatus, source)
    if row is None:
        return Freshness(source, "never", None, None, now)
    return Freshness(
        source,
        _state(row.last_success_at, row.last_error_at, stale, now),
        row.last_success_at,
        row.last_error,
        now,
    )


def all_of(db: DB, now: datetime | None = None) -> dict[str, Freshness]:
    return {s: of(db, s, now) for s in STALE_AFTER}
