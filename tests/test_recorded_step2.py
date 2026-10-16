"""Parses real recorded Step 2 responses, if any were captured with
scripts/record_step2_fixtures.py (skipped otherwise)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pitwall.services import fia_docs, news, session_data
from pitwall.sources.fia import parse_listing

FIX = Path(__file__).parent / "fixtures"
OPENF1 = sorted((FIX / "openf1" / "recorded").glob("race_control_*.json"))
WEATHER = sorted((FIX / "openf1" / "recorded").glob("weather_*.json"))
FEEDS = sorted((FIX / "news" / "recorded").glob("*.xml"))
FIA_LISTING = FIX / "fia" / "recorded" / "listing.html"
FIA_DOCS = FIX / "fia" / "recorded" / "documents.json"


@pytest.mark.skipif(not OPENF1, reason="no recorded OpenF1 race control")
@pytest.mark.parametrize("path", OPENF1, ids=lambda p: p.stem)
def test_recorded_race_control_parses(path):
    rows = json.loads(path.read_text())
    msgs = session_data.parse_race_control(rows, session_id=1)
    assert len(msgs) == len({(r["date"], r["message"]) for r in rows if r.get("message")})
    session_data.stats(msgs)  # must not raise


@pytest.mark.skipif(not WEATHER, reason="no recorded OpenF1 weather")
@pytest.mark.parametrize("path", WEATHER, ids=lambda p: p.stem)
def test_recorded_weather_summarises(path):
    rows = json.loads(path.read_text())
    cond = session_data.summarize_weather(rows, 1, None)
    if rows:
        assert cond.track_min is not None and cond.track_min <= cond.track_max
        assert 0 <= cond.humidity_avg <= 100


@pytest.mark.skipif(not FEEDS, reason="no recorded news feeds")
@pytest.mark.parametrize("path", FEEDS, ids=lambda p: p.stem)
def test_recorded_feed_parses(path):
    items = news.parse_feed(path.read_bytes())
    assert items, f"{path.name}: no headlines found"
    assert all(i.link.startswith("http") for i in items)


@pytest.mark.skipif(not FIA_LISTING.exists(), reason="no recorded FIA documents page")
def test_recorded_fia_listing_parses():
    refs = parse_listing(FIA_LISTING.read_text(), "https://www.fia.com/")
    assert refs and all(r.url.endswith(".pdf") for r in refs)


@pytest.mark.skipif(not FIA_DOCS.exists(), reason="no recorded FIA documents")
def test_recorded_fia_documents_parse_or_fail_cleanly():
    for meta in json.loads(FIA_DOCS.read_text()):
        text = fia_docs.extract_text((FIA_DOCS.parent / meta["file"]).read_bytes())
        kind = fia_docs.classify(meta["title"])
        try:
            if kind == "decision":
                fia_docs.parse_decision(text)
            elif kind == "pu_usage":
                fia_docs.parse_pu_usage(text)
        except fia_docs.ParseError:
            pass  # a clean refusal is fine; anything else is a bug
