# Pitwall — F1 Notifier for the Homelab

Build spec for Claude Code. Put this file in the root of a new, empty repo and tell Claude Code:

> Read PITWALL_BUILD_SPEC.md and build the project in phases. Stop after each phase so I can test it.

---

## 1. What this is

Pitwall is a self-hosted, Docker-based app that tracks Formula 1 and notifies the owner about it.

- **Runs on:** Ubuntu Server VM (host `mh-flex-01`) with Docker and Docker Compose.
- **Install path:** `/opt/pitwall`.
- **Users:** one person (the owner). There is no multi-user auth, but the web UI must be protected (see §9).
- **Default timezone:** `America/New_York`. It must be configurable.

**Owner priorities, in order:**
1. Session start times
2. Location
3. Track info
4. Standings
5. One-off facts and trivia

**Notification channels:**
- Email (SMTP)
- Webhooks (via Apprise)
- Google Calendar via a subscribable **ICS feed**

**No SMS/text.** No Google Calendar API or OAuth.

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Web/API | FastAPI + Jinja2 templates (server-rendered, minimal JS; HTMX is fine) |
| Scheduler | APScheduler (in-process, persistent job store in the DB) |
| Database | SQLite via SQLAlchemy 2.x + Alembic migrations (volume-mounted) |
| HTTP client | httpx with retries/backoff |
| Notifications | Apprise (one `notify()` path for email and all webhook types) |
| ICS | `icalendar` library |
| Charts (dashboard) | Chart.js from a CDN, or server-rendered SVG |
| Tests | pytest, with recorded API fixtures (no live network in tests) |
| Packaging | Single Dockerfile (slim Python base, non-root user) + `docker-compose.yml` |

## 3. Data sources

1. **Jolpica F1 API** (`https://api.jolpi.ca/ergast/f1/`). This is the Ergast-compatible successor.
   - Used for: schedule and sessions, results, qualifying, sprint, driver and constructor standings, circuits, and historical seasons.
   - It is the primary source.
2. **OpenF1 API** (`https://api.openf1.org/v1/`).
   - Used for: post-session per-driver detail such as laps, stints/tyres, and pit stops.
   - Use only historical/post-session data. Don't depend on live timing.
3. **Open-Meteo** (`https://api.open-meteo.com/`).
   - Used for: weather forecasts at the circuit's lat/long for each session.
   - Phase 3.
4. **Curated circuit data** (`data/circuits.yaml`, committed to the repo).
   - Holds static facts the APIs don't provide: length (km), laps, turns, DRS zones, first GP year, official race lap record (driver, time, year), and circuit map image URL or local file.
   - Populate it for every circuit on the current calendar.
   - Mark each entry with `verified: false`.
   - Add a README note telling the owner to spot-check the values.
   - Never invent a value you're unsure of. Leave it `null` instead.

**API etiquette:**
- Respect each API's documented rate limits.
- Cache responses in the DB.
- Use a descriptive User-Agent.
- Use exponential backoff on 429/5xx.
- On any API failure, keep serving cached data and log a warning. A data outage must never crash the app or send garbage notifications.

## 4. Data model (initial)

- `season` (year)
- `circuit` (id, name, locality, country, lat, long, utc_offset/tz name, plus fields merged from circuits.yaml)
- `event` (season, round, name, circuit_id, is_sprint_weekend, date range, status: scheduled/cancelled)
- `session` (event_id, type: FP1/FP2/FP3/SQ/SPRINT/Q/RACE, start_utc, end_utc_est, last_changed_at)
- `driver`, `constructor`
- `result` (session_id, driver, constructor, grid, position, status, points, fastest_lap)
- `standings_snapshot` (season, round, type: driver/constructor, entity_id, position, points, wins)
- `followed_driver` (driver_id)
- `notification_log` (type, channel, payload hash, sent_at, status). Used for dedup and history.
- `spoiler_state` (event_id, watched: bool, watched_at)
- `setting` (key/value). Holds UI-editable settings.

## 5. Scheduled jobs

| Job | When | What |
|---|---|---|
| Schedule sync | Every 6h | Refresh calendar and sessions. **Detect changes** (time moved, session added or cancelled) → send a "Schedule change" alert and regenerate the ICS. |
| Session reminders | Generated from session start times | Default: 24h before quali, 1h before quali, 24h before race, 1h before race. Configurable per session type. Always reschedule when session times change. |
| Race-week preview | Monday 9:00 AM local of race week | Rich email/webhook (see §6). |
| Results sync | Poll every 30 min, starting 2h after a session ends, until results appear (max 24h) | Store results, then trigger post-session alerts, subject to spoiler mode. |
| Standings update | After race (and sprint) results land | Snapshot standings, compute deltas, send a standings alert. |
| Weekly digest (optional) | Configurable day/time | Summary instead of individual alerts, if enabled. |
| Historical backfill | First run, then nightly | Pull past seasons needed for trivia (default: all seasons since 2000, configurable). |

**Dedup rule:** never send the same notification twice. Hash the type, event, session, and content, then check `notification_log`.

## 6. Features

### Phase 1 — Core (MVP)

**Start times**
- Every session, shown in the owner's timezone **and** track-local time.
- Sprint weekends flagged.
- Session reminders (see §5).
- Schedule-change detection and alerts.
- "Odd hours" flag, e.g. "Race starts at 3:00 AM your time".

**Location**
- City, country, and circuit name.
- Track-local timezone.

**Track**
- Everything in circuits.yaml.
- Computed from history:
  - last year's winner and pole sitter
  - winningest *active* driver and constructor at the circuit
  - pole-to-win conversion rate
  - average positions gained from grid to finish by the winner

**Standings**
- Driver and constructor tables.
- Position change since last race (▲/▼/–).
- Gap to leader and to the car ahead.

**ICS feed**
- `GET /calendar.ics`: all sessions.
- `GET /calendar.ics?sessions=race,q`: filtered feed.
- Event titles like `🏁 Race — Mexico City GP`, with location set to the circuit name.
- Description: local time and a link to the app's event page.
- Stable UIDs, so updates modify existing events instead of duplicating them.
- Increment `SEQUENCE` on changes.
- Feed URLs include a secret token (`/calendar.ics?token=...`), because Google needs a public URL to subscribe.

**Notifications**
- Apprise URLs configured via env/settings, e.g. `mailtos://...` and `json://...`/`discord://...`/`ntfy://...`.
- **Routing table:** each alert type maps to a set of channels. For example, previews go to email, and reminders and results go to the webhook.
- **Quiet hours:** alerts that fall in the window are held and delivered when it ends. Session reminders whose session has already started by then are dropped.
- **Spoiler mode:** results and standings alerts are held until the owner clicks "Watched" on the event page (or a configurable auto-release after N hours). Reminders and previews are never held.
- HTML email template plus a plain-text fallback. Webhook payload as JSON with a `type` field and a human-readable `text` field.

**Web UI**
- Dashboard: next session countdown, current standings, followed drivers.
- Event page: sessions, track info, results, "Watched" button.
- Settings page: timezone, followed drivers, reminder offsets, routing, quiet hours, spoiler mode, and a test-notification button per channel.
- Notification history page.

**Ops**
- `GET /health` returns JSON with DB status, last successful sync per source, and the scheduler state.
- Structured logs to stdout.

### Phase 2 — Stats and trivia

**Title math**
- From the remaining rounds and the maximum points available (account for sprint points), compute:
  - who is mathematically eliminated
  - who can clinch at the next race, and the condition, e.g. "Clinches with P3 or better if X finishes below P5"
- Include this in race-week previews when relevant.
- Must be unit-tested with known historical title deciders.

**Teammate head-to-head**
- Qualifying record, race record, and points split.

**Followed-driver tracker**
- Per-race results, average grid vs average finish, positions gained, DNFs, and points trend chart.
- From OpenF1, per race: best lap, tyre stints, pit count.

**Trivia engine**
- Generate facts from the DB only, so every fact is traceable to data. Never hard-code facts or have the code invent them.
- Fact generators:
  - **Milestones:** upcoming Nth start (50/100/150/200/250/300+), first win or podium possible, records within reach
  - **Streaks:** consecutive points finishes, podiums, front-row starts, DNFs
  - **On this day:** results at this GP N years ago
  - **Home race:** for drivers and constructors, based on nationality
  - **Rookie/veteran:** age, starts, career points
- Each fact has a type, a text, and a source query reference.
- Previews include the 3–5 most interesting facts. Rank by a simple score and avoid repeating a fact used in the last few previews.

### Phase 3 — Nice-to-haves

- Weather per session via Open-Meteo, shown in previews. Flag rain risk ≥ 40% on quali or the race.
- Circuit map image in previews.
- Weekly digest mode.
- Export: standings and a followed-driver stats CSV.

## 7. Race-week preview content (the flagship notification)

1. Header: GP name, circuit, city, country, and sprint weekend badge.
2. Session table in owner time and local time, with odd-hours flags.
3. Track card: length, laps, turns, DRS zones, lap record, last year's winner and pole.
4. Standings snapshot: top 5 drivers and top 5 constructors with gaps, plus followed drivers' positions.
5. Title math, if relevant.
6. Followed-driver form: last 3 results.
7. "Did you know?": 3–5 trivia facts.
8. Weather (phase 3).
9. Footer: link to the event page.

## 8. Configuration

- `.env` (with `.env.example` committed). Contents:
  - `TZ`
  - `PITWALL_BASE_URL`
  - `PITWALL_ADMIN_PASSWORD`
  - `PITWALL_ICS_TOKEN`
  - `APPRISE_URLS_EMAIL`
  - `APPRISE_URLS_WEBHOOK`
  - backfill start year
  - log level
- Everything else is editable in the Settings UI and stored in the DB. Env values act as defaults.
- Secrets are never logged and never shown in full in the UI.

## 9. Security

- The web UI requires login: a single admin password from env, with a session cookie, CSRF protection on forms, and rate-limited login.
- The ICS endpoint is protected by its token only, with no login, so Google can fetch it.
- `/health` is unauthenticated but exposes no secrets.
- Run as a non-root user in the container. The only writable path is the data volume.
- The app is designed to sit behind the owner's existing reverse proxy. Document both a direct-port setup and a proxy setup.

## 10. Docker / deployment

- `docker-compose.yml` with one service, `pitwall`:
  - restart `unless-stopped`
  - volume `./data:/app/data` (SQLite DB and cache)
  - port `8080`, configurable
  - a healthcheck hitting `/health`
- Alembic migrations run automatically on container start.
- The first run triggers the historical backfill in the background. The UI shows progress and stays usable.
- **README must include**, written for someone not deeply familiar with Git/GitHub:
  1. Exact commands to clone into `/opt/pitwall`, copy `.env.example`, and `docker compose up -d`
  2. How to set up SMTP email (with a Gmail app-password example and an M365 example) and webhook Apprise URLs
  3. How to subscribe to the ICS feed in Google Calendar ("From URL"), plus a note that Google refreshes subscribed feeds slowly (up to ~24h), so time-sensitive changes come through email/webhook
  4. How to update: `git pull && docker compose up -d --build`
  5. How to back up: copy `./data`
  6. A note to verify `data/circuits.yaml`

## 11. Quality bar

- Type hints throughout, ruff-clean.
- pytest coverage for:
  - title math
  - schedule-change detection
  - reminder rescheduling
  - dedup
  - quiet hours
  - spoiler hold/release
  - ICS UID stability
  - trivia generators
- Tests use recorded API fixtures.
- Graceful degradation everywhere: missing data means a section is omitted, never a crash or a wrong number.
- All times are stored in UTC and converted only for display.

## 12. Build order

1. Skeleton: FastAPI, DB, migrations, Docker, health endpoint, login.
2. Jolpica sync (schedule, results, standings) and circuits.yaml.
3. ICS feed.
4. Apprise notifications, routing, reminders, schedule-change alerts, quiet hours, spoiler mode.
5. Race-week preview and dashboard/settings UI.
6. **→ Phase 1 checkpoint.** Stop and let the owner test it on the server.
7. Phase 2: title math, head-to-head, followed-driver tracker, OpenF1, trivia engine.
8. **→ Phase 2 checkpoint.**
9. Phase 3 items.
