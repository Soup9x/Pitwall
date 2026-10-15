"""Record real Jolpica responses into tests/fixtures/jolpica/recorded/.

The committed fixtures are hand-built in the exact Jolpica format because the API wasn't
reachable from the environment Pitwall was built in. Run this on a machine with internet access
to capture real responses; tests/test_recorded_fixtures.py then checks that Pitwall parses them.

    python scripts/record_fixtures.py            # current season
    python scripts/record_fixtures.py 2024 2025  # specific seasons
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pitwall import USER_AGENT

BASE = "https://api.jolpi.ca/ergast/f1"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "jolpica" / "recorded"


def fetch(client: httpx.Client, path: str) -> dict:
    for attempt in range(5):
        resp = client.get(f"{BASE}/{path}", params={"limit": 100})
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2**attempt)
            continue
        resp.raise_for_status()
        time.sleep(0.5)  # stay well inside Jolpica's rate limits
        return resp.json()
    raise RuntimeError(f"gave up on {path}")


def main() -> None:
    years = [int(y) for y in sys.argv[1:]] or [datetime.now(UTC).year]
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for year in years:
            for name, path in [
                (f"schedule_{year}", f"{year}.json"),
                (f"results_{year}_last", f"{year}/last/results.json"),
                (f"qualifying_{year}_last", f"{year}/last/qualifying.json"),
                (f"driver_standings_{year}", f"{year}/driverStandings.json"),
                (f"constructor_standings_{year}", f"{year}/constructorStandings.json"),
            ]:
                data = fetch(client, path)
                (OUT / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
                print(f"recorded {name}")


if __name__ == "__main__":
    main()
