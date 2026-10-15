"""Time helpers. Everything is stored in UTC and converted only for display."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def safe_zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def in_window(t: time, start: time, end: time) -> bool:
    """True if wall-clock time t is inside [start, end), handling windows that wrap midnight."""
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def next_window_end(now_utc: datetime, tz: ZoneInfo, start: time, end: time) -> datetime | None:
    """If now is inside the window, return the UTC instant the window ends; else None."""
    local = now_utc.astimezone(tz)
    if not in_window(local.time(), start, end):
        return None
    end_local = datetime.combine(local.date(), end, tzinfo=tz)
    if end_local <= local:
        end_local = datetime.combine(local.date() + timedelta(days=1), end, tzinfo=tz)
    return end_local.astimezone(UTC)


def fmt_local(dt: datetime | None, tz: ZoneInfo | None, fmt: str = "%a %d %b, %-I:%M %p") -> str:
    if dt is None or tz is None:
        return "—"
    return dt.astimezone(tz).strftime(fmt)


def fmt_tz_abbr(dt: datetime | None, tz: ZoneInfo | None) -> str:
    if dt is None or tz is None:
        return ""
    return dt.astimezone(tz).strftime("%Z")


def is_odd_hour(dt: datetime | None, tz: ZoneInfo, window: tuple[time, time]) -> bool:
    if dt is None:
        return False
    return in_window(dt.astimezone(tz).time(), *window)


def humanize_delta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def offset_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day" + ("s" if d != 1 else "")
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{minutes} min"
