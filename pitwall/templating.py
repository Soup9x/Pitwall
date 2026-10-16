"""Shared Jinja2 environment for web pages and notification emails."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pitwall.services.standings import fmt_points
from pitwall.timeutil import offset_label
from pitwall.units import fmt_temp, fmt_temp_range, fmt_wind

TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent / "web" / "static"


@lru_cache(maxsize=128)
def _fingerprint(path: str, mtime_ns: int) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """'/static/style.css?v=<content hash>': a new URL whenever the file changes, so browsers
    and the service worker never keep using an old stylesheet or script after an update."""
    try:
        mtime = (STATIC_DIR / path).stat().st_mtime_ns
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={_fingerprint(path, mtime)}"


@lru_cache
def get_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pts"] = fmt_points
    env.filters["offset_label"] = offset_label
    env.filters["temp"] = fmt_temp
    env.filters["temp_range"] = fmt_temp_range
    env.filters["wind"] = fmt_wind
    env.globals["static_url"] = static_url
    return env


def render(name: str, **ctx: object) -> str:
    return get_jinja_env().get_template(name).render(**ctx)
