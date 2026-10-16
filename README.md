# Pitwall

A self-hosted Formula 1 notifier for the homelab. Pitwall tracks the F1 calendar and tells you
about it by email, webhook (Discord, ntfy, generic JSON, and anything else
[Apprise](https://github.com/caronc/apprise) supports) and a Google Calendar feed.

**Status: all three phases of `PITWALL_BUILD_SPEC.md` are built**: the core notifier, stats
and trivia, and weather, maps, digest and CSV export.

## What it does (Phase 1)

- **Session times** for every session, in your timezone *and* the track's local time. Sprint
  weekends are flagged, and so are "odd hours" sessions (e.g. "Race starts at 3:00 AM your time").
- **Reminders** before sessions. The default is 24 h and 1 h before qualifying and the race.
  You can change them per session type.
- **Schedule-change alerts** when a session moves, is added or is cancelled. Reminders and the
  calendar feed update automatically.
- **Race-week preview** every Monday at 9:00 AM. It covers sessions, track facts, last year's
  winner and pole, standings and your followed drivers' form.
- **Results and standings alerts** after qualifying, sprints and races, with ▲/▼ position changes
  and the gap to the leader and to the car ahead.
- **Track stats** from history: winningest active driver/team, pole-to-win rate, and the
  winner's average positions gained.
- **Spoiler mode** holds results and standings alerts until you click **Watched**, and hides
  the same results on the pages. It can also release them automatically after N hours.
- **Quiet hours** hold alerts until morning. Reminders for sessions that started in the
  meantime are dropped.
- **ICS calendar feed** for Google Calendar, with stable event IDs, so a moved session
  updates the existing entry instead of creating a duplicate.
- **Web UI** with a dashboard, calendar, event pages, standings, notification history and settings.

## Stats and trivia (Phase 2)

- **Title math** on the Standings page and in race-week previews, when it matters. It shows who
  is mathematically out and what the leader needs to clinch at the next race, e.g. "Clinches
  with P3 or better if Piastri doesn't win" (sprint weekends and the constructors' title use
  points margins instead). Conditions are always safe: the leader's own fastest-lap point is
  never assumed, and a tie only counts for the leader when the rival can't match their wins.
- **Teammate head-to-head** (Head-to-head page): qualifying record, race record (races where
  both retired don't count) and points split, including sprints.
- **Driver pages** (click any driver name): race-by-race results, average grid and finish,
  positions gained, DNFs and a points-trend chart. For drivers you follow, best lap, tyre stints
  and pit stops come from [OpenF1](https://openf1.org) (2023 onwards), fetched in the
  background after each race.
- **"Did you know?" facts** on event pages and in previews, computed only from stored results:
  - milestones (50th/100th… start, first win or podium in reach, circuit records)
  - streaks (points, podiums, front rows, DNFs)
  - "N years ago" winners at this race
  - home races for drivers and teams
  - youngest/oldest driver and rookies

  Each fact records the query it came from (hover the ⓘ on the event page). Career totals are
  only stated when Pitwall has the driver's whole career loaded; the default history (since
  2000) covers the current grid's careers. Previews show the 3–5 highest-scoring facts and
  skip ones used in the last three previews.

## Weather, maps, digest and exports (Phase 3)

- **Weather per session** from [Open-Meteo](https://open-meteo.com):
  - covers sessions up to 16 days ahead, refreshed every 3 hours and just before the Monday
    preview;
  - shown on the dashboard, event pages and in previews (temperature in °C/°F, chance of
    rain);
  - a **rain risk** of 40% or more for qualifying or the race is flagged with ☔.
- **Circuit maps** on event pages and in previews. Pitwall uses, in order:
  1. the `map_image` URL or file from `data/circuits.yaml`;
  2. an image you put in `data/maps/<circuitId>.png` (or `.jpg`, `.webp`, `.svg`);
  3. otherwise, a map it draws itself from OpenF1 car-position data for the fastest lap of
     the most recent race at that circuit (2023 onwards).

  Maps are served at `/circuit-maps/<circuitId>` without login so email clients can load
  them. As with the calendar feed, that only works in email if Pitwall is reachable from the
  internet.
- **Weekly digest** (Settings → Weekly digest):
  - When it's on, the alert types you pick (schedule changes, results, standings) are held
    and sent as one summary at your chosen day and time.
  - The summary also covers the coming week's sessions (with rain risk) and the top of the
    standings.
  - Reminders and previews always go out on time.
  - Choose where the digest goes under Routing ("Weekly digest").
- **CSV export** (links under the Standings tables and on driver pages):
  - driver and constructor standings;
  - race-by-race stats for your followed drivers, including OpenF1 best lap, pit stops and
    tyres.

## Penalties, tyres, race control, conditions and news

- **Penalty watch** (menu → Penalties):
  - power unit elements used per driver, against the season allocation;
  - grid penalties per event (places or back of the grid, with the reason);
  - super licence penalty points over a rolling 12 months, with expiry dates (12 = race ban).

  Admins enter values under **Penalties → Edit / sources**, and manual values always win over
  parsed ones. Pitwall can also read the FIA's event documents (PDFs). Paste the season's FIA
  documents page there once. During race week it's checked every 2 hours from Wednesday, and
  every 30 minutes on Friday and Saturday (track time). Parsing is deliberately strict: if a
  document looks wrong (an unknown car, a usage count that went down, impossible numbers),
  nothing from it is stored and it's listed as "couldn't parse - view source". Every parsed
  value links to its document.

  Alerts: a grid penalty is announced; a driver reaches 10 or 11 licence points (or 12); and,
  off by default, a followed driver takes a new power unit element.
- **Tyre nominations** (hard / medium / soft, C1–C6) on event pages and in the race-week
  preview. "Not announced yet" until known, with last year's choice at the same circuit.
  Admins pick the three compounds on the event page, or paste a Pirelli press release link:
  it's only saved if the release names exactly one hard, medium and soft.
- **Race control** (2023 onwards, from OpenF1, after each session): a collapsible, filterable
  timeline on the event page with safety cars, VSCs, red flags, laps under SC/VSC,
  investigations and penalties per session. The track card shows real safety car history
  ("SC in 4 of last 5 races here"), and it feeds the trivia.
- **Measured conditions** (2023 onwards, from OpenF1): min/max/average track and air
  temperature, humidity, rain and maximum wind per session, plus a track-temperature chart.
  Next to the forecast: "Last year here: track 41–52 °C, dry". Choose °C, °F or both under
  Settings → Time.
- **News** from RSS feeds (menu → News, plus a strip on the dashboard). Pitwall stores only
  each headline's title, link, source, date and tags, never article text. Headlines are tagged
  with followed drivers, teams, the upcoming circuit and track-change keywords. Track-change
  headlines about the next circuit appear as **Track updates** on its event page and in the
  preview, and raise a "Track-change news" alert. Admins edit the feeds and keywords under
  Settings → News feeds.

Spoiler mode covers all of this. While an event is held, its race control, the penalty
decisions and headlines published after its first competitive session started are left out
of pages and emails. Pages show "N headlines hidden" and a **Show anyway** link.

Data comes from the [Jolpica F1 API](https://github.com/jolpica/jolpica-f1) (the
Ergast successor), [OpenF1](https://openf1.org), FIA event documents, Pirelli press releases
and the news feeds you choose, plus curated circuit facts in `data/circuits.yaml`.

---

## 1. Install on the server

### Quick install (recommended)

Run this on `mh-flex-01`. The installer asks you a few questions and does the rest:

```bash
curl -fsSL https://raw.githubusercontent.com/soup9x/pitwall/main/install.sh | sudo bash
```

If you already have a copy of the code, run `sudo ./install.sh` from inside the folder instead.
This also works if the repository is private (clone it first, logging in when git asks).

The installer:

1. checks for Docker and offers to install it if it's missing,
2. downloads Pitwall to `/opt/pitwall`,
3. asks for:
   - your timezone, port and the address you'll use on your network
   - a password for the `admin` account (or generates one)
   - how far back to download history
4. writes `.env` (readable only by root) and starts Pitwall,
5. prints your web address and login.

Then log in as **admin**. Add where alerts should go under **Settings → Notification
channels** (section 2), and invite other people under **Users** (section 8).

Run it again at any time to update Pitwall or change settings. Your current answers are the
defaults, and the old `.env` is backed up first. Options: `--dir` (install location),
`--branch` (git branch, default `main`) and `--repo`.

### Manual install

These commands assume Ubuntu with Docker and Docker Compose already installed. Run them on
`mh-flex-01`.

```bash
# Get the code into /opt/pitwall
sudo git clone https://github.com/soup9x/pitwall.git /opt/pitwall
cd /opt/pitwall

# Make the data folder writable by the container's user (UID 1000)
sudo chown -R 1000:1000 data

# Create your settings file from the example
sudo cp .env.example .env
sudo nano .env
```

In `.env`, set at least these:

| Setting | What to put |
|---|---|
| `PITWALL_ADMIN_PASSWORD` | A long password for the web UI |
| `PITWALL_ICS_TOKEN` | A random string. Generate one with `openssl rand -hex 24` |
| `PITWALL_BASE_URL` | How you reach Pitwall, e.g. `http://mh-flex-01:8080` |
| `TZ` | Your timezone (default `America/New_York`) |
| `APPRISE_URLS_EMAIL` / `APPRISE_URLS_WEBHOOK` | See section 2 |

Save the file (in nano: `Ctrl+O`, `Enter`, `Ctrl+X`), then start Pitwall:

```bash
sudo docker compose up -d
```

Open `http://mh-flex-01:8080` and log in with your password. On first start, Pitwall:

1. applies database migrations,
2. downloads this season's calendar (within about 15 seconds),
3. starts downloading past seasons in the background (default: since 2000). A progress bar
   appears at the top of the page. The UI stays usable the whole time. Jolpica rate-limits
   requests, so this can take an hour or two. Track stats fill in as it goes.

Check that it's healthy:

```bash
sudo docker compose ps          # STATUS should say "healthy" after a minute
sudo docker compose logs -f     # live logs (Ctrl+C to stop watching)
curl http://localhost:8080/health
```

Then go to **Settings**, pick the drivers you follow, and press **Send test** for each channel.

## 2. Notifications (email and webhooks)

Each user adds their own destinations in the web UI under **Settings → Notification channels**. Choose a type and fill
in the form, then press **Add** and **Test**. You can add as many as you like, and disable or
remove them at any time. Pitwall builds the [Apprise](https://github.com/caronc/apprise/wiki) URL
for you, and passwords are never shown again after you save them.

### Email

| Type | What you need |
|---|---|
| **Gmail** | Your address and an **app password**. Turn on 2-Step Verification, then create one at <https://myaccount.google.com/apppasswords>. Your normal password won't work. Google Workspace addresses work too. |
| **Microsoft 365 / Outlook.com** | Your address and password. For Microsoft 365, an admin must enable **Authenticated SMTP** for the mailbox. |
| **Other SMTP server** | Server, port (587 STARTTLS or 465 SSL), username, password and a From address. |
| **Apprise URL** | Anything else Apprise supports. |

By default, alerts go to the sending address; fill in **Send alerts to** to use another one.
Emails are HTML with a plain-text alternative part.

### Webhooks

| Type | What you need |
|---|---|
| **ntfy** | Nothing. Leave the topic blank to get a random, hard-to-guess one. Install the ntfy app and subscribe to the topic shown after saving. A self-hosted server works too. |
| **Discord** | Server Settings → Integrations → Webhooks → New Webhook → **Copy Webhook URL**, then paste it in. |
| **Generic JSON** | Your endpoint's URL (Home Assistant, n8n, your own app). |
| **Apprise URL** | Any other service Apprise supports (Slack, Telegram, Pushover, …). |

Generic JSON webhooks receive Pitwall's own payload:

```json
{"type": "reminder", "title": "🏁 Race in 1 hour — Mexico City GP",
 "text": "Race — Mexico City Grand Prix\nYour time: Sun 26 Oct, 4:00 PM EDT\n…",
 "url": "http://mh-flex-01:8080/events/42", "data": {"session": "RACE", "…": "…"}}
```

`type` is one of `reminder`, `schedule_change`, `preview`, `results`, `standings`, `test`.
Other services such as Discord and ntfy get the human-readable `text`.

### Advanced: Apprise URLs in `.env`

You can also list Apprise URLs in `.env` (`APPRISE_URLS_EMAIL`, `APPRISE_URLS_WEBHOOK`,
comma-separated). They're used in addition to the channels added in the UI and appear there as
read-only. Apply changes with `sudo docker compose up -d`.

### Routing, quiet hours and spoiler mode

Under **Settings → Routing**, you choose which channels each alert type uses. The default is:
previews by email, reminders/results/standings by webhook, and schedule changes on both.
Quiet hours and spoiler mode are in Settings too.

**How spoiler mode works.** With it on, the latest event's results stay hidden until you mark
that event **Watched** on its page. If you set "Auto-release after N hours", they also come out
N hours after the event's latest results arrived. Each new session restarts that clock, so a
race isn't revealed early by its qualifying. Alerts and pages follow the same rule. While an
event is held:
- its results, and the standings, recent form, head-to-head, driver season pages, title maths
  and trivia (which all include the latest results) are left out of the pages entirely, not
  just collapsed, so they aren't in the page source either;
- race control, penalty decisions and news headlines published after the event's first
  competitive session (sprint qualifying, qualifying, sprint or race) started are left out
  too. That includes the time before results arrive, e.g. news during qualifying;
- the race-week preview email and weekly digest leave out standings, recent form and trivia;
- CSV exports for that season answer "hidden by spoiler mode".

Each hidden section has a **Show anyway** link, which reveals it for that one page view only.

## 3. Google Calendar (ICS feed)

Every user has a private feed link under **Account** in the web UI. It looks like this:

```
https://pitwall.example.com/calendar.ics?token=…
```

Session times in the event descriptions use that user's timezone. **Create a new link** on the
Account page makes the old one stop working.

To include only some sessions, add `&sessions=race,q`. Other values: `sprint`, `sq`,
`practice`, `fp1`, `fp2`, `fp3`.

To subscribe in Google Calendar (on a computer):

1. Next to **Other calendars**, click **+** → **From URL**.
2. Paste the feed URL and click **Add calendar**.

**Google must be able to reach the URL from the internet.** A LAN address such as
`http://mh-flex-01:8080` only works inside your network, so Google Calendar can't subscribe to
it. Calendar apps on your network (e.g. Apple Calendar or Thunderbird on a home computer) can.
The feed only answers with a valid token.

> **Note:** Google refreshes subscribed calendars slowly, sometimes only once a day. Time
> changes may take up to ~24 h to show in Google Calendar. That is why schedule changes are also
> sent by email/webhook, and why reminders come from Pitwall rather than from the calendar.

## 4. Updating

Run `sudo ./install.sh` in `/opt/pitwall` and choose **Keep my current settings**. Or do it
by hand:

```bash
cd /opt/pitwall
sudo git pull
sudo docker compose up -d --build
```

Database migrations run automatically on start.

## 5. Backups

Everything lives in `/opt/pitwall/data`: the database, the settings and `circuits.yaml`. Back
up that folder, plus your `.env`.

```bash
cd /opt/pitwall
sudo docker compose stop
sudo tar czf ~/pitwall-backup-$(date +%F).tgz data .env
sudo docker compose start
```

To restore, put `data/` and `.env` back in `/opt/pitwall` and run `sudo docker compose up -d`.

## 6. Accessing Pitwall

Pitwall serves plain HTTP on port 8080 of the server. Browse to `http://<server IP>:8080` (or
`http://mh-flex-01:8080`) from any device on your network. Change the host port with
`PITWALL_PORT` in `.env`, and set `PITWALL_BASE_URL` to the address you use so links in alerts
point to it.

## 7. Please check `data/circuits.yaml`

The curated track facts include length, laps, turns, DRS zones, first GP and lap record. They
were compiled without live verification, and **every entry is marked `verified: false`**.
Values we weren't sure about were left `null` on purpose, and Pitwall hides those fields instead
of guessing. Please spot-check the file against formula1.com, fix anything wrong, and set
`verified: true`. Edits are picked up at the next schedule sync (every 6 h) or on restart
(`sudo docker compose restart`).

Facts are keyed by circuit, not Grand Prix name. The 2026 Bahrain GP ran at Sepang, so there's
a `sepang` entry next to `bahrain`. Its race laps and lap record are left empty because they
depend on the 2026 race; fill them in once you've checked them.

If you've edited `circuits.yaml` yourself, `git pull` may stop with a conflict on it. Keep your
copy with `sudo git stash`, `sudo git pull`, then `sudo git stash pop`, and fix any conflict.

---

## 8. Users and invites

Everyone shares the F1 data. Each person has their own:
- login and timezone
- followed drivers
- email and webhook channels
- reminder times, quiet hours, spoiler mode and weekly digest
- calendar feed

**Creating an account (admins):**
1. Open **Users → Create an account**, enter a username and either type a password or leave
   it blank to generate one (like `k7Fp-3xQa-Wm9t`, with no look-alike characters).
2. Pitwall shows the address, username and password once. Press **Copy login details** and
   send them to the person.
3. With "They choose their own password" ticked (the default), their first login takes them
   to **Account**, and they can't use the rest of Pitwall until they pick their own password.

**Inviting someone (admins):** alternatively, **Users → Create invite link** makes a one-time
link (valid 7 days) where the person picks their own username and password.

**Managing users (admins):**
- **Forgotten password or locked out:** **Temporary password** next to the person gives them a
  new generated password (shown once), unlocks the account and signs them out everywhere;
  they choose their own at next login. **Reset link** instead creates a one-time link valid
  for 48 hours.
- You can also make someone an admin, or disable or delete their account.
- Admins also see system settings (history download, data sync) and can view everyone's
  notification history.

**Your own account:** under **Account** you can change your password, get your calendar link,
or sign out everywhere.

**If you were using Pitwall before multi-user support:** your existing settings, channels,
followed drivers and calendar link now belong to the user **admin**. The admin password is the
one from `.env` (`PITWALL_ADMIN_PASSWORD`). After that first start, passwords are managed in
Pitwall, so editing `.env` no longer changes them.

**Locked out of every admin account?** Run:

```bash
cd /opt/pitwall
sudo docker compose exec pitwall python -m pitwall set-password admin
```

This also unlocks the account. To create an account from the command line:
`... python -m pitwall create-user NAME` (add `--admin` for an admin). For either command,
press Enter at the password prompt to generate one; it's printed, and the person chooses their
own at first login.

## Security notes

- **Logins:**
  - Every page needs a login.
  - Passwords are hashed with scrypt and must be at least 10 characters.
  - Sessions end when you change your password or an admin disables you.
  - Every form has CSRF protection.
- **Brute-force protection:**
  - Logins are rate-limited per visitor (5 failures per 15 minutes).
  - Each account also locks for 15 minutes after 5 wrong passwords.
  - Error messages never reveal whether a username exists.
- **Accounts:** there's no open sign-up. Admins create accounts or send one-time invite links
  (only a hash of the link is stored). Passwords an admin chose or generated are shown once,
  never stored in plain text, and (by default) replaced by the person at their first login.
- **Public URLs:** `/calendar.ics` uses a per-user token instead of a login, so Google can fetch
  it. `/circuit-maps/*` is public, so email clients can show maps. `/health` tells internet
  visitors only `{"status": "ok"}`; details are for logged-in users and the local healthcheck.
- **Browser hardening:**
  - Strict Content-Security-Policy with no inline scripts.
  - `X-Frame-Options: DENY`, `nosniff` and a no-referrer-leak policy.
  - Pages aren't cached, and search engines are asked not to index them.
- Secrets are redacted from logs, and the UI shows them only masked. Notification-channel
  passwords added in the UI live in the database in `./data`, which is created readable only by
  the container's user. Treat backups of `./data` like your `.env`.
- The container runs as a non-root user with a read-only filesystem. Only `./data` is writable.

## Troubleshooting

- **Can't log in:** the username for the original account is `admin`. After 5 wrong attempts
  the account locks for 15 minutes. To reset a password, see section 8; editing `.env` no
  longer changes it.
- **"Permission denied" on `/app/data` in the logs:** run `sudo chown -R 1000:1000 data`, or set
  `PUID`/`PGID` in `.env` to the folder's owner.
- **Test notification fails:** the error appears at the top of the Settings page and on the
  Notifications page. For Gmail, make sure you used an *app password*.
- **No data / API outage:** Pitwall keeps showing cached data and retries automatically. The last
  successful sync is shown at `/health`.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                 # tests use recorded/hand-built API fixtures; no network needed
ruff check . && ruff format --check .
PITWALL_DATA_DIR=./data PITWALL_ADMIN_PASSWORD=dev python -m pitwall serve
```

Other commands: `python -m pitwall migrate | sync | backfill`.
`scripts/record_fixtures.py` records real Jolpica responses for tests (see
`tests/fixtures/jolpica/README.md`).
`scripts/record_step2_fixtures.py` does the same for OpenF1 race control/weather, the news
feeds and (given the FIA documents page) FIA documents. Run it on the server, where there's
internet access:

```bash
cd /opt/pitwall
sudo docker compose run --rm --no-deps --user root -v "$PWD:/src" -w /src \
    --entrypoint python pitwall scripts/record_step2_fixtures.py 2025 'https://www.fia.com/...'
```

`tests/test_recorded_step2.py` then checks that Pitwall parses everything it recorded.
