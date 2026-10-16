"""Temperature formatting in the user's chosen unit: "C", "F" or "both"."""

from __future__ import annotations

TEMP_UNITS = ("C", "F", "both")


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def fmt_temp(c: float | None, unit: str = "both") -> str:
    if c is None:
        return ""
    if unit == "C":
        return f"{round(c)} °C"
    if unit == "F":
        return f"{round(c_to_f(c))} °F"
    return f"{round(c)} °C / {round(c_to_f(c))} °F"


def fmt_temp_range(lo: float | None, hi: float | None, unit: str = "both") -> str:
    """A range like '41-52 °C' (en dash), or °F, or both. Empty if either end is unknown."""
    if lo is None or hi is None:
        return ""
    c = f"{round(lo)}–{round(hi)} °C"
    f = f"{round(c_to_f(lo))}–{round(c_to_f(hi))} °F"
    return {"C": c, "F": f}.get(unit, f"{c} / {f}")


def fmt_wind(ms: float | None) -> str:
    if ms is None:
        return ""
    return f"{round(ms * 3.6)} km/h"
