"""The shipped data/circuits.yaml: parses, uses real timezones and well-typed values."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from pitwall.models import Circuit
from pitwall.services.circuits import apply_curated

YAML = Path(__file__).resolve().parents[1] / "data" / "circuits.yaml"


def test_every_entry_is_well_formed():
    data = yaml.safe_load(YAML.read_text())
    for cid, facts in data.items():
        assert isinstance(facts, dict), cid
        ZoneInfo(facts["timezone"])
        assert isinstance(facts["verified"], bool), cid
        for key in ("length_km", "laps", "turns", "drs_zones", "first_gp_year"):
            assert facts.get(key) is None or isinstance(facts[key], (int, float)), (cid, key)


def test_sepang_sits_alongside_bahrain():
    data = yaml.safe_load(YAML.read_text())
    assert "bahrain" in data and "sepang" in data
    assert data["sepang"]["verified"] is False
    circuit = Circuit(id="sepang", name="Sepang International Circuit")
    apply_curated(circuit, data["sepang"])
    assert circuit.tz_name == "Asia/Kuala_Lumpur"
    assert circuit.first_gp_year == 1999 and circuit.length_km == 5.543
    # Unknown values stay empty rather than guessed (the UI hides them).
    assert circuit.laps is None and circuit.lap_record_time is None
