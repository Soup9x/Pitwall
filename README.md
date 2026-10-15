# Pitwall

A self-hosted Formula 1 notifier for the homelab. Pitwall tracks the F1 calendar and tells you
about it by email, webhook (Discord, ntfy, generic JSON, and anything else
[Apprise](https://github.com/caronc/apprise) supports) and a Google Calendar feed.

**Status: Phase 1 (core) is done.** Phase 2 (title math, head-to-head, trivia) and Phase 3
(weather, digest, CSV export) come next. See `PITWALL_BUILD_SPEC.md` for the full plan.

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
- **Spoiler mode** holds results and standings alerts until you click **Watched**. It can also
  release them automatically after N hours.
- **Quiet hours** hold alerts until morning. Reminders for sessions that started in the
  meantime are dropped.
- **ICS calendar feed** for Google Calendar, with stable event IDs, so a moved session
  updates the existing entry instead of creating a duplicate.
- **Web UI** with a dashboard, calendar, event pages, standings, notification history and settings.

Data comes from the [Jolpica F1 API](https://github.com/jolpica/jolpica-f1) (the
Ergast successor) plus curated circuit facts in `data/circuits.yaml`.

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
   - your timezone and port
   - whether Pitwall sits behind a reverse proxy
   - a web UI password (or generates one)
   - how far back to download history
4. writes `.env` (readable only by root) and starts Pitwall,
5. prints your web address, password (if generated) and calendar-feed URL.

Then log in and add where alerts should go under **Settings → Notification channels**
(see section 2).

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

Add destinations in the web UI under **Settings → Notification channels**. Choose a type and fill
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

## 3. Google Calendar (ICS feed)

Your feed URL is:

```
http://mh-flex-01:8080/calendar.ics?token=YOUR_PITWALL_ICS_TOKEN
```

To include only some sessions, add `&sessions=race,q`. Other values: `sprint`, `sq`,
`practice`, `fp1`, `fp2`, `fp3`.

To subscribe in Google Calendar (on a computer):

1. Next to **Other calendars**, click **+** → **From URL**.
2. Paste the feed URL and click **Add calendar**.

**Google must be able to reach the URL from the internet.** A `mh-flex-01` address only works
inside your network, so publish Pitwall through your reverse proxy (section 6) and use that
address. Only `/calendar.ics` needs to be reachable, and it only answers with the right token.

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

## 6. Running behind a reverse proxy

**Direct port (no proxy).** This is the default. Browse to `http://mh-flex-01:8080`. Change
the host port with `PITWALL_PORT` in `.env`.

**Behind your existing proxy.** Point the proxy at `http://mh-flex-01:8080` (or
`127.0.0.1:8080` if it runs on the same host). Then in `.env`:

```
PITWALL_BASE_URL=https://pitwall.example.com
PITWALL_SECURE_COOKIES=true
FORWARDED_ALLOW_IPS=<your proxy's IP>   # so Pitwall sees real client IPs (login rate limiting)
```

If the proxy runs on the same machine, you can stop exposing the port to the network. Set
`PITWALL_BIND=127.0.0.1` in `.env`; the installer does this when you say the proxy is local.

Caddy example:

```
pitwall.example.com {
    reverse_proxy mh-flex-01:8080
}
```

nginx example:

```nginx
location / {
    proxy_pass http://mh-flex-01:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 7. Please check `data/circuits.yaml`

The curated track facts include length, laps, turns, DRS zones, first GP and lap record. They
were compiled without live verification, and **every entry is marked `verified: false`**.
Values we weren't sure about were left `null` on purpose, and Pitwall hides those fields instead
of guessing. Please spot-check the file against formula1.com, fix anything wrong, and set
`verified: true`. Edits are picked up at the next schedule sync (every 6 h) or on restart
(`sudo docker compose restart`).

---

## Security notes

- The web UI needs the admin password. Logins are rate-limited (5 failures per 15 min), and
  every form has CSRF protection.
- `/calendar.ics` uses its token instead of a login, so Google can fetch it.
- `/health` needs no login and returns no secrets.
- Secrets are redacted from logs, and the UI shows them only masked. Notification-channel
  passwords added in the UI live in the database in `./data`, which is created readable only by
  the container's user. Treat backups of `./data` like your `.env`.
- The container runs as a non-root user with a read-only filesystem. Only `./data` is writable.

## Troubleshooting

- **Can't log in:** check `PITWALL_ADMIN_PASSWORD` in `.env`, then run
  `sudo docker compose up -d`. After 5 wrong attempts, wait 15 minutes.
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
