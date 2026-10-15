"""Loads data/circuits.yaml and merges curated facts into circuit rows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session as DB

from pitwall.config import get_env
from pitwall.models import Circuit

log = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parents[2]


def circuits_yaml_path() -> Path | None:
    candidates = [
        get_env().data_dir / "circuits.yaml",
        _PKG_ROOT / "data" / "circuits.yaml",
        Path("/app/seed/circuits.yaml"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def load_curated() -> dict[str, dict[str, Any]]:
    path = circuits_yaml_path()
    if path is None:
        log.warning("circuits.yaml not found; track facts will be omitted")
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not parse circuits.yaml", extra={"error": str(exc)})
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _num(v: Any, cast: type) -> Any:
    if v is None:
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def apply_curated(circuit: Circuit, facts: dict[str, Any]) -> None:
    circuit.tz_name = facts.get("timezone") or circuit.tz_name
    circuit.length_km = _num(facts.get("length_km"), float)
    circuit.laps = _num(facts.get("laps"), int)
    circuit.turns = _num(facts.get("turns"), int)
    circuit.drs_zones = _num(facts.get("drs_zones"), int)
    circuit.first_gp_year = _num(facts.get("first_gp_year"), int)
    rec = facts.get("lap_record") or {}
    circuit.lap_record_driver = rec.get("driver")
    circuit.lap_record_time = _num(rec.get("time"), str)
    circuit.lap_record_year = _num(rec.get("year"), int)
    circuit.map_image = facts.get("map_image")
    circuit.facts_verified = bool(facts.get("verified", False))


def merge_all(db: DB, curated: dict[str, dict[str, Any]] | None = None) -> int:
    curated = load_curated() if curated is None else curated
    n = 0
    for circuit in db.query(Circuit).all():
        facts = curated.get(circuit.id)
        if facts:
            apply_curated(circuit, facts)
            n += 1
    return n
