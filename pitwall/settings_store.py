"""UI-editable settings stored in the `setting` table. Env values act as defaults."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env
from pitwall.models import OWNER_ID, Setting, UserSetting

ALERT_TYPES = ("reminder", "schedule_change", "preview", "results", "standings", "digest")
ALERT_LABELS = {
    "reminder": "Session reminders",
    "schedule_change": "Schedule changes",
    "preview": "Race-week preview",
    "results": "Session results",
    "standings": "Standings updates",
    "digest": "Weekly digest",
}
# Alert types that can be folded into the weekly digest instead of being sent one by one.
DIGESTIBLE = ("schedule_change", "results", "standings")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CHANNELS = ("email", "webhook")
SPOILER_TYPES = frozenset({"results", "standings"})


def _default_routing() -> dict[str, list[str]]:
    return {
        "reminder": ["webhook"],
        "schedule_change": ["email", "webhook"],
        "preview": ["email"],
        "results": ["webhook"],
        "standings": ["webhook"],
        "digest": ["email"],
    }


def _default_offsets() -> dict[str, list[int]]:
    # Minutes before session start.
    return {
        "FP1": [],
        "FP2": [],
        "FP3": [],
        "SQ": [],
        "SPRINT": [],
        "Q": [1440, 60],
        "RACE": [1440, 60],
    }


@dataclass
class AppSettings:
    timezone: str = "America/New_York"
    reminder_offsets: dict[str, list[int]] = field(default_factory=_default_offsets)
    routing: dict[str, list[str]] = field(default_factory=_default_routing)
    quiet_enabled: bool = False
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    spoiler_enabled: bool = False
    spoiler_auto_release_hours: int = 0  # 0 = never auto-release
    odd_start: str = "00:00"
    odd_end: str = "07:00"
    backfill_start_year: int = 2000
    digest_enabled: bool = False
    digest_day: str = "mon"
    digest_time: str = "08:00"
    digest_types: list[str] = field(default_factory=lambda: list(DIGESTIBLE))

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("America/New_York")

    @property
    def quiet_window(self) -> tuple[time, time] | None:
        if not self.quiet_enabled:
            return None
        return parse_hhmm(self.quiet_start), parse_hhmm(self.quiet_end)

    @property
    def odd_window(self) -> tuple[time, time]:
        return parse_hhmm(self.odd_start), parse_hhmm(self.odd_end)

    def channels_for(self, alert_type: str) -> list[str]:
        return [c for c in self.routing.get(alert_type, []) if c in CHANNELS]


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def defaults() -> AppSettings:
    env = get_env()
    return AppSettings(timezone=env.tz, backfill_start_year=env.backfill_start_year)


# Settings that apply to the whole install (admin-only); everything else is per user.
SYSTEM_KEYS = frozenset({"backfill_start_year"})


def load_settings(db: DB, user_id: int | None = None) -> AppSettings:
    """Defaults, then install-wide settings, then the user's own (default: the owner)."""
    s = defaults()
    rows = {r.key: r.value for r in db.scalars(select(Setting).where(Setting.key.in_(SYSTEM_KEYS)))}
    uid = user_id or OWNER_ID
    rows.update(
        {r.key: r.value for r in db.scalars(select(UserSetting).where(UserSetting.user_id == uid))}
    )
    for key, raw in rows.items():
        if not hasattr(s, key):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if key == "reminder_offsets" and isinstance(value, dict):
            merged = _default_offsets()
            merged.update({k: [int(x) for x in v] for k, v in value.items()})
            value = merged
        if key == "routing" and isinstance(value, dict):
            merged_r = _default_routing()  # alert types added later get their defaults
            merged_r.update(value)
            value = merged_r
        setattr(s, key, value)
    return s


def save_settings(db: DB, updates: dict[str, Any], user_id: int | None = None) -> AppSettings:
    """Install-wide keys go to `setting`; the rest are saved for the user (default: owner)."""
    known = asdict(defaults())
    uid = user_id or OWNER_ID
    for key, value in updates.items():
        if key not in known:
            raise KeyError(key)
        if key in SYSTEM_KEYS:
            set_kv(db, key, value)
        else:
            set_user_kv(db, uid, key, value)
    db.flush()
    return load_settings(db, uid)


def get_user_kv(db: DB, user_id: int, key: str, default: Any = None) -> Any:
    row = db.get(UserSetting, (user_id, key))
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return default


def set_user_kv(db: DB, user_id: int, key: str, value: Any) -> None:
    row = db.get(UserSetting, (user_id, key))
    if row is None:
        db.add(UserSetting(user_id=user_id, key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)


def get_kv(db: DB, key: str, default: Any = None) -> Any:
    """Internal (non-settings) key/value state, e.g. backfill progress."""
    row = db.get(Setting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return default


def set_kv(db: DB, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)
