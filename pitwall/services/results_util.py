"""Shared helpers for reading race results."""

from __future__ import annotations

from pitwall.models import Result

# Statuses that mean the driver never took the start.
NON_STARTS = frozenset(
    {
        "Did not start",
        "Did not qualify",
        "Did not prequalify",
        "Withdrew",
        "Not classified ... did not start",
        "Excluded from qualifying",
    }
)


def classified(r: Result) -> bool:
    return bool(r.position_text and r.position_text.isdigit())


def started(r: Result) -> bool:
    return (r.status or "") not in NON_STARTS


def finished(r: Result) -> bool:
    """Took the chequered flag (including lapped cars)."""
    status = r.status or ""
    return status == "Finished" or status.startswith("+") or status == "Lapped"


def dnf(r: Result) -> bool:
    return started(r) and not finished(r)
