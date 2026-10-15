"""Shared Jinja2 environment for web pages and notification emails."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pitwall.services.standings import fmt_points
from pitwall.timeutil import offset_label

TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"


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
    return env


def render(name: str, **ctx: object) -> str:
    return get_jinja_env().get_template(name).render(**ctx)
