# Jolpica fixtures

These files are in the exact Jolpica/Ergast JSON format, but they were **built by hand**: the
API wasn't reachable from the sandbox Pitwall was developed in, so nothing could be recorded.
Results for the 2022–2024 Mexico City GPs (winners, poles) are real but trimmed to a few rows.
The 2025 "mini season" (three rounds) and its standings are synthetic test data.

To record real responses, run `python scripts/record_fixtures.py` on a machine with internet
access. Recorded files go to `recorded/`, and `tests/test_recorded_fixtures.py` checks that
Pitwall can parse them.
