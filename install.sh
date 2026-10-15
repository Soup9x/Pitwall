#!/usr/bin/env bash
# Pitwall one-shot installer.
#
#   sudo ./install.sh                      # from a copy of the repo
#   curl -fsSL https://raw.githubusercontent.com/soup9x/pitwall/main/install.sh | sudo bash
#
# It checks/installs Docker, gets the code into /opt/pitwall, asks you for the settings Pitwall
# needs, writes .env, starts the container and sends test notifications. Safe to re-run: your
# current settings are offered as defaults, or you can keep them and just update/restart.
#
# Options:  --dir DIR      install location (default /opt/pitwall)
#           --branch NAME  git branch to install (default main)
#           --repo URL     git repository (default https://github.com/soup9x/pitwall.git)

# The whole script is one { ... } block so bash reads it completely before running anything:
# safe when piped from curl, and when the git update below rewrites this file mid-run.
{
set -euo pipefail

BRANCH_SET=0
[[ -n "${PITWALL_BRANCH:-}" ]] && BRANCH_SET=1
REPO_URL="${PITWALL_REPO:-https://github.com/soup9x/pitwall.git}"
BRANCH="${PITWALL_BRANCH:-main}"
INSTALL_DIR="${PITWALL_DIR:-/opt/pitwall}"
APP_UID=1000
APP_GID=1000

# ------------------------------------------------------------------------------ output helpers

if [[ -t 2 ]]; then
  B=$'\e[1m' D=$'\e[2m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' C=$'\e[36m' N=$'\e[0m'
else
  B="" D="" R="" G="" Y="" C="" N=""
fi
say() { printf '%s\n' "$*" >&2; }
step() { printf '\n%s==> %s%s\n' "$B$C" "$*" "$N" >&2; }
ok() { printf '%s✓%s %s\n' "$G" "$N" "$*" >&2; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*" >&2; }
die() { printf '%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
hint() { printf '%s%s%s\n' "$D" "$*" "$N" >&2; }

usage() {
  say "Usage: sudo ./install.sh [--dir DIR] [--branch NAME] [--repo URL]"
  say "Interactive installer for Pitwall. See the top of install.sh for details."
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch needs a value}"; BRANCH_SET=1; shift 2 ;;
    --repo) REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
    -h|--help) usage ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

# Answers come from the terminal even when this script itself is piped in via curl | bash.
if [[ -n "${PITWALL_INSTALL_STDIN:-}" || -t 0 ]]; then
  exec 3<&0
elif { exec 3</dev/tty; } 2>/dev/null; then
  :
else
  die "No terminal to ask questions on. Download the script and run: sudo bash install.sh"
fi

# ------------------------------------------------------------------------------ prompt helpers

# ask "Question" "default" -> echoes the answer (default if left blank)
ask() {
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    printf '%s %s[%s]%s: ' "$prompt" "$D" "$default" "$N" >&2
  else
    printf '%s: ' "$prompt" >&2
  fi
  IFS= read -r reply <&3 || die "Input ended unexpectedly."
  printf '%s' "${reply:-$default}"
}

# ask_required "Question" "default"
ask_required() {
  local v
  while :; do
    v="$(ask "$1" "${2:-}")"
    [[ -n "$v" ]] && { printf '%s' "$v"; return; }
    warn "This one is required."
  done
}

# ask_secret "Question" -> hidden input
ask_secret() {
  local reply
  printf '%s: ' "$1" >&2
  if [[ -t 3 ]]; then
    IFS= read -rs reply <&3 || die "Input ended unexpectedly."
    printf '\n' >&2
  else
    IFS= read -r reply <&3 || die "Input ended unexpectedly."
  fi
  printf '%s' "$reply"
}

# ask_yn "Question" y|n -> exit status 0 for yes
ask_yn() {
  local q="$1" def="${2:-y}" hint_txt reply
  [[ "$def" == y ]] && hint_txt="Y/n" || hint_txt="y/N"
  while :; do
    printf '%s %s[%s]%s: ' "$q" "$D" "$hint_txt" "$N" >&2
    IFS= read -r reply <&3 || die "Input ended unexpectedly."
    reply="${reply:-$def}"
    case "${reply,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) warn "Please answer y or n." ;;
    esac
  done
}

# choose "Question" "default number" "option 1" "option 2" ... -> echoes the chosen number
choose() {
  local q="$1" def="$2" i reply
  shift 2
  say "$q"
  for ((i = 1; i <= $#; i++)); do say "  $i) ${!i}"; done
  while :; do
    reply="$(ask "Choose 1-$#" "$def")"
    if [[ "$reply" =~ ^[0-9]+$ ]] && ((reply >= 1 && reply <= $#)); then
      printf '%s' "$reply"
      return
    fi
    warn "Please enter a number from 1 to $#."
  done
}

urlencode() {
  local LC_ALL=C s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

mask() {
  local v="$1"
  if [[ -z "$v" ]]; then printf '(not set)'
  elif ((${#v} <= 8)); then printf '****'
  else printf '%s…' "${v:0:6}"
  fi
}

# Masks the credentials in an Apprise URL list for display: scheme://host/…
mask_urls() {
  local blob="$1" out="" u rest host
  IFS=',' read -ra parts <<<"$blob"
  for u in "${parts[@]}"; do
    [[ -z "$u" ]] && continue
    rest="${u#*://}"
    host="${rest%%\?*}"; host="${host%%/*}"; host="${host##*@}"
    out+="${out:+, }${u%%://*}://${host}/…"
  done
  printf '%s' "${out:-(none)}"
}

random_hex() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

env_get() {  # env_get KEY -> value from the existing .env (quotes stripped)
  local file="$INSTALL_DIR/.env" line v
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^$1=" "$file" | tail -n1 || true)"
  v="${line#*=}"
  if [[ "$v" == \'*\' || "$v" == \"*\" ]]; then v="${v:1:${#v}-2}"; fi
  printf '%s' "$v"
}

# ------------------------------------------------------------------------------ 0. root

if [[ $EUID -ne 0 ]]; then
  if [[ -f "${BASH_SOURCE[0]:-}" ]] && command -v sudo >/dev/null; then
    exec sudo PITWALL_REPO="$REPO_URL" PITWALL_BRANCH="$BRANCH" PITWALL_DIR="$INSTALL_DIR" \
      bash "${BASH_SOURCE[0]}" "$@"
  fi
  die "Please run as root, e.g.:  curl -fsSL <url>/install.sh | sudo bash"
fi

say ""
say "${B}🏁  Pitwall installer${N}"
hint "Press Enter to accept the value in [brackets]. Ctrl+C quits at any time; nothing is"
hint "started until you confirm the summary at the end of the questions."

# ------------------------------------------------------------------------------ 1. prerequisites

step "Checking prerequisites"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "System: ${PRETTY_NAME:-unknown}"
  [[ "${ID:-}" == ubuntu || "${ID:-}" == debian || "${ID_LIKE:-}" == *debian* ]] ||
    warn "This script is tested on Ubuntu/Debian; other systems may need manual steps."
fi

missing=()
for tool in git curl openssl; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if ((${#missing[@]})); then
  if command -v apt-get >/dev/null; then
    say "Installing: ${missing[*]}"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" ca-certificates >/dev/null
  else
    die "Please install: ${missing[*]}"
  fi
fi
ok "git, curl and openssl are available"

if ! command -v docker >/dev/null; then
  warn "Docker is not installed."
  if ask_yn "Install Docker now using Docker's official script (get.docker.com)?" y; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker >/dev/null 2>&1 || true
  else
    die "Docker is required. Install it, then run this script again."
  fi
fi
if ! docker compose version >/dev/null 2>&1; then
  warn "The Docker Compose plugin is missing."
  if command -v apt-get >/dev/null && ask_yn "Install docker-compose-plugin with apt?" y; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin >/dev/null ||
      die "Couldn't install docker-compose-plugin. See https://docs.docker.com/compose/install/linux/"
  else
    die "Docker Compose v2 is required (the 'docker compose' command)."
  fi
fi
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Try: sudo systemctl start docker"
ok "$(docker --version | cut -d, -f1) with Compose $(docker compose version --short 2>/dev/null || echo v2)"

# ------------------------------------------------------------------------------ 2. code

step "Getting Pitwall"

script_dir=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$script_dir" && -f "$script_dir/docker-compose.yml" && -d "$script_dir/pitwall" &&
      "$script_dir" != "$INSTALL_DIR" ]]; then
  say "You're running the installer from a copy of Pitwall in $script_dir."
  if ask_yn "Install from this folder (instead of $INSTALL_DIR)?" y; then
    INSTALL_DIR="$script_dir"
  fi
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  ok "Found an existing install in $INSTALL_DIR"
  current_branch="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if ((!BRANCH_SET)) && [[ -n "$current_branch" && "$current_branch" != HEAD ]]; then
    BRANCH="$current_branch"
  fi
  if ask_yn "Update it to the latest version from git (branch $BRANCH)?" y; then
    git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" ||
      die "Couldn't fetch branch '$BRANCH'. Check your network, or use --branch."
    git -C "$INSTALL_DIR" checkout --quiet "$BRANCH" 2>/dev/null ||
      git -C "$INSTALL_DIR" checkout --quiet -b "$BRANCH" "origin/$BRANCH"
    git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$BRANCH" ||
      die "Couldn't update: $INSTALL_DIR has local changes. Run 'git -C $INSTALL_DIR status' to see them."
    ok "Updated to $(git -C "$INSTALL_DIR" log -1 --format='%h %s')"
  fi
elif [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
  die "$INSTALL_DIR exists and isn't empty (and isn't a git checkout). Move it aside or use --dir."
else
  say "Cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
  if ! git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"; then
    hint "If the repository is private, clone it yourself (git will ask you to log in), then run"
    hint "  sudo ./install.sh  from inside the folder."
    die "git clone failed."
  fi
  ok "Cloned to $INSTALL_DIR"
fi

[[ -f "$INSTALL_DIR/docker-compose.yml" ]] ||
  die "Branch '$BRANCH' doesn't contain the Pitwall app yet. Merge it into main, or re-run with --branch <name>."
cd "$INSTALL_DIR"

# ------------------------------------------------------------------------------ 3. settings

configure=1
if [[ -f .env ]]; then
  step "Settings"
  say "Found existing settings in $INSTALL_DIR/.env."
  c="$(choose "What would you like to do?" 1 \
    "Keep my current settings and just (re)start Pitwall" \
    "Go through the questions again (current values are the defaults)")"
  [[ "$c" == 1 ]] && configure=0
fi

if ((configure)); then
  # --- timezone
  step "1/7 Timezone"
  tz_default="$(env_get TZ)"
  [[ -z "$tz_default" ]] && tz_default="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [[ -z "$tz_default" || "$tz_default" == UTC || "$tz_default" == Etc/UTC ]] && tz_default="America/New_York"
  hint "Session times are shown in this timezone. Examples: America/New_York, America/Chicago,"
  hint "America/Los_Angeles, Europe/London. (Can be changed later in Settings.)"
  while :; do
    TZ_VAL="$(ask "Your timezone" "$tz_default")"
    if [[ ! -d /usr/share/zoneinfo ]] || [[ -f "/usr/share/zoneinfo/$TZ_VAL" && "$TZ_VAL" != *..* ]]; then
      break
    fi
    warn "'$TZ_VAL' isn't a known timezone name."
  done

  # --- network
  step "2/7 Network"
  while :; do
    prev="$(env_get PITWALL_PORT)"
    PORT="$(ask "Port for the web UI" "${prev:-8080}")"
    [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) && break
    warn "Please enter a port number."
  done
  if command -v ss >/dev/null && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q . &&
     ! docker ps --format '{{.Names}}' | grep -qx pitwall; then
    warn "Something is already listening on port $PORT. Pitwall won't start unless you free it or pick another port."
  fi

  host_name="$(hostname -s 2>/dev/null || hostname)"
  BIND="0.0.0.0"
  SECURE="false"
  FWD="127.0.0.1"
  prev_proxy=n
  [[ "$(env_get PITWALL_SECURE_COOKIES)" == true || "$(env_get PITWALL_BIND)" == 127.0.0.1 ]] && prev_proxy=y
  hint "A reverse proxy (Caddy, nginx, Traefik, Nginx Proxy Manager…) is also how Google Calendar"
  hint "can reach the calendar feed from the internet."
  if ask_yn "Will Pitwall sit behind a reverse proxy?" "$prev_proxy"; then
    prev_url="$(env_get PITWALL_BASE_URL)"
    [[ "$prev_url" == https://* ]] || prev_url=""
    while :; do
      BASE_URL="$(ask_required "Public URL you'll use (e.g. https://pitwall.example.com)" "$prev_url")"
      [[ "$BASE_URL" =~ ^https?://[^/[:space:]]+ ]] && break
      warn "Please enter a full URL starting with https:// (or http://)."
    done
    BASE_URL="${BASE_URL%/}"
    [[ "$BASE_URL" == https://* ]] && SECURE="true"
    if ask_yn "Does the proxy run on this same machine?" y; then
      BIND="127.0.0.1"
      FWD="*"
      hint "Pitwall will only listen on 127.0.0.1:$PORT; point the proxy at http://127.0.0.1:$PORT"
    else
      hint "Point the proxy at http://$host_name:$PORT"
      prev="$(env_get FORWARDED_ALLOW_IPS)"
      [[ "$prev" == 127.0.0.1 ]] && prev=""
      FWD="$(ask "IP address of the proxy (lets Pitwall see real client IPs; * = trust any)" "${prev:-*}")"
    fi
  else
    prev_url="$(env_get PITWALL_BASE_URL)"
    [[ -z "$prev_url" || "$prev_url" == https://* ]] && prev_url="http://$host_name:$PORT"
    BASE_URL="$(ask "Address you'll type in the browser" "$prev_url")"
    BASE_URL="${BASE_URL%/}"
  fi

  # --- password
  step "3/7 Web UI password"
  ADMIN_PW="$(env_get PITWALL_ADMIN_PASSWORD)"
  GENERATED_PW=""
  if [[ -n "$ADMIN_PW" && "$ADMIN_PW" != change-me* ]] && ask_yn "Keep your current password?" y; then
    :
  else
    hint "At least 8 characters. Leave blank to generate a strong one."
    while :; do
      pw1="$(ask_secret "Password")"
      if [[ -z "$pw1" ]]; then
        ADMIN_PW="$(random_hex 12)"
        GENERATED_PW="$ADMIN_PW"
        ok "Generated a password (shown at the end)"
        break
      fi
      if ((${#pw1} < 8)); then warn "Too short."; continue; fi
      if [[ "$pw1" == *"'"* ]]; then warn "Please don't use the ' character."; continue; fi
      pw2="$(ask_secret "Type it again")"
      [[ "$pw1" == "$pw2" ]] || { warn "They don't match."; continue; }
      ADMIN_PW="$pw1"
      break
    done
  fi
  ICS_TOKEN="$(env_get PITWALL_ICS_TOKEN)"
  [[ -z "$ICS_TOKEN" ]] && ICS_TOKEN="$(random_hex 24)"
  SECRET_KEY="$(env_get PITWALL_SECRET_KEY)"

  # --- email
  step "4/7 Email notifications"
  EMAIL_URLS="$(env_get APPRISE_URLS_EMAIL)"
  opts=("Gmail (needs an app password)" "Microsoft 365 / Outlook.com" "Other SMTP server"
        "Paste an Apprise URL" "No email")
  def=5
  if [[ -n "$EMAIL_URLS" ]]; then
    opts=("Keep current: $(mask_urls "$EMAIL_URLS")" "${opts[@]}")
    def=1
  fi
  c="$(choose "How should Pitwall send email?" "$def" "${opts[@]}")"
  [[ -n "$EMAIL_URLS" ]] && c=$((c - 1))  # shift so 0 = keep
  case "$c" in
    0) : ;;
    1)
      hint "Create an app password at https://myaccount.google.com/apppasswords"
      hint "(2-Step Verification must be on). It's 16 letters; spaces don't matter."
      addr="$(ask_required "Your Gmail address")"
      while :; do
        app_pw="$(ask_secret "App password")"
        app_pw="${app_pw// /}"
        [[ -n "$app_pw" ]] && break
      done
      [[ "$app_pw" =~ ^[a-zA-Z]{16}$ ]] || warn "That doesn't look like a 16-letter app password; using it anyway."
      to="$(ask "Send notifications to" "$addr")"
      user="${addr%@*}" domain="${addr#*@}"
      if [[ "${domain,,}" == gmail.com || "${domain,,}" == googlemail.com ]]; then
        EMAIL_URLS="mailto://$(urlencode "$user"):$(urlencode "$app_pw")@gmail.com"
        [[ "$to" != "$addr" ]] && EMAIL_URLS+="?to=$(urlencode "$to")"
      else  # Google Workspace address
        EMAIL_URLS="mailtos://$(urlencode "$addr"):$(urlencode "$app_pw")@$domain?smtp=smtp.gmail.com&mode=starttls&to=$(urlencode "$to")"
      fi
      ;;
    2)
      hint "Microsoft 365 needs 'Authenticated SMTP' enabled for the mailbox (admin setting)."
      addr="$(ask_required "Your Microsoft 365 / Outlook.com address")"
      pw="$(ask_secret "Password (or app password)")"
      to="$(ask "Send notifications to" "$addr")"
      domain="${addr#*@}"
      smtp="smtp.office365.com"
      [[ "${domain,,}" == outlook.com || "${domain,,}" == hotmail.com || "${domain,,}" == live.com ]] && smtp="smtp-mail.outlook.com"
      EMAIL_URLS="mailtos://$(urlencode "$addr"):$(urlencode "$pw")@$domain?smtp=$smtp&mode=starttls&to=$(urlencode "$to")"
      ;;
    3)
      host="$(ask_required "SMTP server (e.g. smtp.example.com)")"
      sport="$(ask "SMTP port" 587)"
      if [[ "$sport" == 465 ]]; then mode=ssl; else mode=starttls; fi
      suser="$(ask_required "SMTP username")"
      pw="$(ask_secret "SMTP password")"
      from="$(ask_required "From address" "$([[ "$suser" == *@* ]] && printf '%s' "$suser")")"
      to="$(ask_required "Send notifications to" "$from")"
      EMAIL_URLS="mailtos://$(urlencode "$suser"):$(urlencode "$pw")@${from#*@}:$sport?smtp=$host&mode=$mode&from=$(urlencode "$from")&to=$(urlencode "$to")"
      ;;
    4)
      hint "See https://github.com/caronc/apprise/wiki — separate several URLs with commas."
      EMAIL_URLS="$(ask_required "Apprise URL")"
      ;;
    5) EMAIL_URLS="" ;;
  esac

  # --- webhooks
  step "5/7 Webhook notifications (phone push, Discord, …)"
  HOOK_URLS="$(env_get APPRISE_URLS_WEBHOOK)"
  if [[ -n "$HOOK_URLS" ]]; then
    say "Current webhooks: $(mask_urls "$HOOK_URLS")"
    ask_yn "Keep them?" y || HOOK_URLS=""
  fi
  while :; do
    c="$(choose "Add a webhook?" 5 "ntfy (free phone push notifications)" "Discord" \
      "Generic JSON webhook (Home Assistant, n8n, your own endpoint)" "Paste an Apprise URL" \
      "Done — no more webhooks")"
    new=""
    case "$c" in
      1)
        server="$(ask "ntfy server" "ntfy.sh")"
        server="${server#https://}" server="${server#http://}" server="${server%/}"
        hint "Anyone who knows the topic name can read it, so make it hard to guess."
        topic="$(ask "Topic name" "pitwall-$(random_hex 5)")"
        if [[ "$server" == ntfy.sh ]]; then new="ntfy://$topic"; else new="ntfys://$server/$topic"; fi
        say "Subscribe to topic ${B}$topic${N} in the ntfy app (server: $server)."
        ;;
      2)
        hint "Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL"
        while :; do
          url="$(ask_required "Discord webhook URL")"
          if [[ "$url" =~ /api/webhooks/([0-9]+)/([A-Za-z0-9_-]+) ]]; then
            new="discord://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
            break
          fi
          warn "That doesn't look like https://discord.com/api/webhooks/<id>/<token>"
        done
        ;;
      3)
        hint "Pitwall will POST JSON like {\"type\": ..., \"title\": ..., \"text\": ...} to this URL."
        while :; do
          url="$(ask_required "Webhook URL (https://…)")"
          case "$url" in
            https://*) new="jsons://${url#https://}"; break ;;
            http://*) new="json://${url#http://}"; break ;;
            *) warn "Please enter a URL starting with http:// or https://" ;;
          esac
        done
        ;;
      4) new="$(ask_required "Apprise URL")" ;;
      5) break ;;
    esac
    [[ -n "$new" ]] && HOOK_URLS="${HOOK_URLS:+$HOOK_URLS,}$new" && ok "Added $(mask_urls "$new")"
  done

  # --- history
  step "6/7 History"
  hint "Past seasons are downloaded once in the background for track stats."
  this_year="$(date +%Y)"
  while :; do
    prev="$(env_get PITWALL_BACKFILL_START_YEAR)"
    START_YEAR="$(ask "Download seasons starting from" "${prev:-2000}")"
    [[ "$START_YEAR" =~ ^[0-9]{4}$ ]] && ((START_YEAR >= 1950 && START_YEAR <= this_year)) && break
    warn "Please enter a year between 1950 and $this_year."
  done
  LOG_LEVEL="$(env_get PITWALL_LOG_LEVEL)"
  LOG_LEVEL="${LOG_LEVEL:-INFO}"

  # --- summary
  step "7/7 Review"
  say "  Install folder:   $INSTALL_DIR"
  say "  Timezone:         $TZ_VAL"
  say "  Web UI address:   $BASE_URL"
  say "  Listens on:       $BIND:$PORT"
  say "  Password:         $([[ -n "$GENERATED_PW" ]] && echo "(generated)" || echo "(set)")"
  say "  Email:            $(mask_urls "$EMAIL_URLS")"
  say "  Webhooks:         $(mask_urls "$HOOK_URLS")"
  say "  Seasons from:     $START_YEAR"
  ask_yn "Save these settings and start Pitwall?" y || die "Cancelled — nothing was changed."

  # --- write .env
  if [[ -f .env ]]; then
    cp -p .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
    hint "Previous settings backed up to .env.bak.*"
  fi
  q() { printf "'%s'" "${1//\'/%27}"; }  # single-quoted: no $-interpolation by Docker Compose
  umask 077
  cat >.env <<EOF
# Written by install.sh on $(date). Re-run sudo ./install.sh to change, or edit by hand
# and apply with: sudo docker compose up -d

TZ=$(q "$TZ_VAL")
PITWALL_BASE_URL=$(q "$BASE_URL")
PITWALL_PORT=$PORT
PITWALL_BIND=$BIND

PITWALL_ADMIN_PASSWORD=$(q "$ADMIN_PW")
PITWALL_ICS_TOKEN=$(q "$ICS_TOKEN")
PITWALL_SECRET_KEY=$(q "$SECRET_KEY")

APPRISE_URLS_EMAIL=$(q "$EMAIL_URLS")
APPRISE_URLS_WEBHOOK=$(q "$HOOK_URLS")

PITWALL_BACKFILL_START_YEAR=$START_YEAR
PITWALL_LOG_LEVEL=$LOG_LEVEL

PITWALL_SECURE_COOKIES=$SECURE
FORWARDED_ALLOW_IPS=$(q "$FWD")

PUID=$APP_UID
PGID=$APP_GID
EOF
  umask 022
  chmod 600 .env
  ok "Saved $INSTALL_DIR/.env"
fi

# ------------------------------------------------------------------------------ 4. start

step "Starting Pitwall"
mkdir -p data
puid="$(env_get PUID)" pgid="$(env_get PGID)"
chown -R "${puid:-$APP_UID}:${pgid:-$APP_GID}" data
docker compose up -d --build || die "docker compose failed — see the messages above."

PORT="$(env_get PITWALL_PORT)"; PORT="${PORT:-8080}"
say "Waiting for Pitwall to come up…"
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done
if ((!healthy)); then
  warn "Pitwall didn't answer on port $PORT within 2 minutes. Last log lines:"
  docker compose logs --tail 40 >&2 || true
  die "Start-up failed. Fix the problem above and run this script again."
fi
ok "Pitwall is running"

# ------------------------------------------------------------------------------ 5. test

for channel in email webhook; do
  key="APPRISE_URLS_$(tr '[:lower:]' '[:upper:]' <<<"$channel")"
  [[ -n "$(env_get "$key")" ]] || continue
  if ask_yn "Send a test $channel notification now?" y; then
    if docker compose exec -T pitwall python -m pitwall test "$channel" </dev/null 2>/dev/null | tail -n1 >&2; then
      ok "Check that it arrived."
    else
      warn "The $channel test failed. Re-run the installer to fix the settings, or see README → Troubleshooting."
    fi
  fi
done

# ------------------------------------------------------------------------------ 6. done

BASE_URL="$(env_get PITWALL_BASE_URL)"
ICS_TOKEN="$(env_get PITWALL_ICS_TOKEN)"
say ""
say "${B}${G}🏁  Pitwall is installed.${N}"
say ""
say "  Web UI:     ${B}$BASE_URL${N}"
if [[ -n "${GENERATED_PW:-}" ]]; then
  say "  Password:   ${B}$GENERATED_PW${N}   ${Y}← write this down; it's also in $INSTALL_DIR/.env${N}"
else
  say "  Password:   the one you chose (stored in $INSTALL_DIR/.env)"
fi
say ""
say "  Calendar feed for Google Calendar (Other calendars → + → From URL):"
say "    ${B}$BASE_URL/calendar.ics?token=$ICS_TOKEN${N}"
hint "    Google must be able to reach this address from the internet (use your proxy's URL),"
hint "    and it refreshes subscribed calendars slowly (up to ~24 h)."
say ""
say "  Next steps:"
say "    • Log in and pick the drivers you follow under Settings."
say "    • Past seasons download in the background (progress bar in the UI)."
say "    • Spot-check $INSTALL_DIR/data/circuits.yaml (see README section 7)."
say ""
say "  Useful commands (run in $INSTALL_DIR):"
say "    sudo docker compose logs -f         # live logs"
say "    sudo ./install.sh                   # update, or change settings"
say "    sudo tar czf ~/pitwall-backup.tgz data .env   # backup"
say ""
exit 0
}
