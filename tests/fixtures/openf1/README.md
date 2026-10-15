Hand-built responses in the OpenF1 API format (https://openf1.org), used by the tests.

None of these were recorded from the live API (it wasn't reachable from the sandbox Pitwall
was built in). They follow the documented field names and types; the values are made up
for the tests (e.g. `weather_9905.json` deliberately contains one impossible 999 °C reading).
Recorded responses, when captured, go in `recorded/`.

- `sessions_2025_race.json`, `laps_*`, `stints_*`, `pit_*`, `location_*`: test_phase2.py
- `sessions_2025_all.json`, `sessions_2024_all.json`, `race_control_*`, `weather_*`:
  test_session_data.py (race control timeline, safety car history, measured conditions)
