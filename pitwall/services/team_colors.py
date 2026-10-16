"""Team colours from data/team_colors.yaml, used only for a thin stripe next to team names."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import yaml

from pitwall.config import get_env

log = logging.getLogger(__name__)
_PKG_ROOT = Path(__file__).resolve().parents[2]
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_cache: tuple[float, dict[int, dict[str, str]]] | None = None


def _path() -> Path | None:
    for p in (
        get_env().data_dir / "team_colors.yaml",
        _PKG_ROOT / "data" / "team_colors.yaml",
        Path("/app/seed/team_colors.yaml"),
    ):
        if p.is_file():
            return p
    return None


def load() -> dict[int, dict[str, str]]:
    """{season: {constructor_id: "#RRGGBB"}}; bad entries are skipped. Re-read every 5 min."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < 300:
        return _cache[1]
    out: dict[int, dict[str, str]] = {}
    path = _path()
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("could not read team_colors.yaml", extra={"error": str(exc)})
            raw = {}
        for season, teams in raw.items():
            if not isinstance(teams, dict):
                continue
            try:
                year = int(season)
            except (TypeError, ValueError):
                continue
            out[year] = {str(k): str(v) for k, v in teams.items() if _HEX.match(str(v))}
    _cache = (time.monotonic(), out)
    return out


def color(constructor_id: str | None, season: int) -> str | None:
    """The team's colour for `season`, else its latest earlier one; None if unknown."""
    if not constructor_id:
        return None
    colours = load()
    for year in sorted((y for y in colours if y <= season), reverse=True):
        if constructor_id in colours[year]:
            return colours[year][constructor_id]
    return None
