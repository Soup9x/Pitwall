"""Record real responses from the Step 2 sources into tests/fixtures/*/recorded/.

The committed OpenF1/FIA/news test fixtures are hand-built (the sources weren't reachable from
the environment Pitwall was built in). Run this somewhere with internet access, e.g. on the
Pitwall server with the app's own image:

    cd /opt/pitwall
    sudo docker compose run --rm --no-deps --user root -v "$PWD:/src" -w /src \\
        --entrypoint python pitwall scripts/record_step2_fixtures.py [YEAR] [FIA_PAGE_URL]

tests/test_recorded_step2.py then checks that Pitwall parses every recorded file. Feed files
are saved with item descriptions/content removed (Pitwall never uses article text).
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pitwall import USER_AGENT
from pitwall.services.news import SEED_FEEDS
from pitwall.sources.fia import parse_listing

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
OPENF1 = "https://api.openf1.org/v1"


def get(client: httpx.Client, url: str, **params: object) -> httpx.Response:
    for attempt in range(4):
        resp = client.get(url, params=params or None)
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 1))
            continue
        resp.raise_for_status()
        time.sleep(0.6)  # stay well inside the sources' rate limits
        return resp
    resp.raise_for_status()
    return resp


def record_openf1(client: httpx.Client, year: int) -> None:
    out = ROOT / "openf1" / "recorded"
    out.mkdir(parents=True, exist_ok=True)
    sessions = get(client, f"{OPENF1}/sessions", year=year).json()
    (out / f"sessions_{year}_all.json").write_text(json.dumps(sessions, indent=1))
    now = datetime.now(UTC)
    races = [
        s
        for s in sessions
        if s.get("session_name") in ("Race", "Qualifying")
        and datetime.fromisoformat(str(s["date_end"]).replace("Z", "+00:00")) < now
    ][-2:]
    for s in races:
        key = s["session_key"]
        for path in ("race_control", "weather"):
            data = get(client, f"{OPENF1}/{path}", session_key=key).json()
            (out / f"{path}_{key}.json").write_text(json.dumps(data, indent=1))
            print(f"recorded openf1 {path} {key} ({len(data)} rows)")


_STRIP = re.compile(r"<(description|content:encoded|content|summary)\b[^>]*>.*?</\1>", re.I | re.S)


def record_feeds(client: httpx.Client) -> None:
    out = ROOT / "news" / "recorded"
    out.mkdir(parents=True, exist_ok=True)
    for name, url in SEED_FEEDS:
        try:
            body = get(client, url).text
        except httpx.HTTPError as exc:
            print(f"FEED FAILED {name} {url}: {exc}")
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        (out / f"{slug}.xml").write_text(_STRIP.sub("", body))
        print(f"recorded feed {name}")


def record_fia(client: httpx.Client, page_url: str, max_pdfs: int = 12) -> None:
    out = ROOT / "fia" / "recorded"
    out.mkdir(parents=True, exist_ok=True)
    page = get(client, page_url).text
    (out / "listing.html").write_text(page)
    refs = parse_listing(page, page_url)
    print(f"listing: {len(refs)} PDF links")
    wanted = [r for r in refs if re.search(r"decision|power unit|offence", r.title, re.I)]
    meta = []
    for i, ref in enumerate(wanted[:max_pdfs]):
        pdf = get(client, ref.url).content
        name = f"doc_{i:02d}.pdf"
        (out / name).write_bytes(pdf)
        meta.append({"file": name, "title": ref.title, "url": ref.url})
        print(f"recorded {ref.title}")
    (out / "documents.json").write_text(json.dumps(meta, indent=1))


def main() -> None:
    args = sys.argv[1:]
    year = int(args[0]) if args and args[0].isdigit() else datetime.now(UTC).year
    fia_page = next((a for a in args if a.startswith("https://")), None)
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as c:
        record_openf1(c, year)
        record_feeds(c)
        if fia_page:
            record_fia(c, fia_page)
        else:
            print("No FIA documents page given; skipping FIA documents.")


if __name__ == "__main__":
    main()
