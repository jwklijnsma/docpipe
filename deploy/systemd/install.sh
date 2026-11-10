#!/usr/bin/env bash
# Install docpipe as a systemd service with Podman Quadlet, in one command.
#
#   sudo ./install.sh                         # rootful system service, starts at boot
#   ./install.sh                              # rootless, as your own user
#   sudo ./install.sh --repos /srv/git --lan  # other folder, reachable from the network
#
# Without a checkout of the repo:
#   curl -fsSL https://raw.githubusercontent.com/jwklijnsma/docpipe/main/deploy/systemd/install.sh | sudo bash -s -- --lan
#
# What it does: creates the access token as a Podman secret (once), writes the settings
# file (once, never overwritten), writes the Quadlet unit with your folder, port and Ollama
# URL, starts the service, waits until it answers and prints the link with the token.
# Run it again to change settings or to update the unit; it is safe to repeat.
set -euo pipefail

APP=docpipe
PORT_IN=8770                      # port inside the container
OLLAMA_VAR=DOCPIPE_OLLAMA_URL             # environment variable holding the Ollama URL
MODEL_VAR=DOCPIPE_MODEL
DEFAULT_REPOS_ROOTFUL=/srv/git
SET_USER=no                 # rootful: run as the owner of the repos folder
RAW=https://raw.githubusercontent.com/jwklijnsma/$APP/main/deploy/systemd

usage() {
  cat <<EOF
Usage: $0 [options]
  --repos DIR      folder with the repositories (default: $DEFAULT_REPOS_ROOTFUL as root, ~/git otherwise)
  --port N         port on the host (default: $PORT_IN)
  --lan            listen on all interfaces instead of 127.0.0.1 only
  --ollama URL     Ollama server (default: Ollama on this host)
  --model NAME     default model (can also be picked in the web UI)
  --new-token      replace the access token
  --uninstall      stop and remove the service (keeps the token, settings and data)
  -h, --help       this help
EOF
}

REPOS="" PORT="$PORT_IN" LAN=0 OLLAMA="" MODEL="" NEW_TOKEN=0 UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repos) REPOS="${2:?--repos needs a folder}"; shift 2 ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --lan) LAN=1; shift ;;
    --ollama) OLLAMA="${2:?--ollama needs a URL}"; shift 2 ;;
    --model) MODEL="${2:?--model needs a name}"; shift 2 ;;
    --new-token) NEW_TOKEN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
command -v podman >/dev/null || die "podman is not installed (dnf install podman / apt install podman)"
command -v systemctl >/dev/null || die "systemd is required"
pv=$(podman version --format '{{.Client.Version}}' 2>/dev/null || podman --version | awk '{print $NF}')
major=${pv%%.*}; rest=${pv#*.}; minor=${rest%%.*}
if [ "${major:-0}" -lt 4 ] || { [ "$major" -eq 4 ] && [ "${minor:-0}" -lt 4 ]; }; then
  die "Podman $pv is too old for Quadlet; 4.4 or newer is needed"
fi

if [ "$(id -u)" -eq 0 ]; then
  MODE=rootful
  UNIT_DIR=/etc/containers/systemd
  CONF_DIR=/etc/$APP
  SYSTEMCTL=(systemctl)
  JFLAG=""
  TEMPLATE=$APP-rootful.container
  : "${REPOS:=$DEFAULT_REPOS_ROOTFUL}"
else
  MODE=rootless
  UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd
  CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/$APP
  SYSTEMCTL=(systemctl --user)
  JFLAG="--user "
  TEMPLATE=$APP.container
  : "${REPOS:=$HOME/git}"
fi
UNIT=$UNIT_DIR/$APP.container
ENV_FILE=$CONF_DIR/$APP.env

if [ "$UNINSTALL" -eq 1 ]; then
  say "Stopping and removing $APP ($MODE)"
  "${SYSTEMCTL[@]}" stop "$APP" 2>/dev/null || true
  rm -f "$UNIT"
  "${SYSTEMCTL[@]}" daemon-reload
  echo "Kept: the Podman secret $APP-token, $ENV_FILE and the $APP-data volume."
  echo "Remove those with: podman secret rm $APP-token; rm -r $CONF_DIR; podman volume rm $APP-data"
  exit 0
fi

# The unit and settings templates: next to this script, or from GitHub.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fetch() {
  if [ -f "$HERE/$1" ]; then cp "$HERE/$1" "$TMP/$1"
  else
    command -v curl >/dev/null || die "$1 not found next to this script, and curl is missing"
    curl -fsSL "$RAW/$1" -o "$TMP/$1" || die "could not download $RAW/$1"
  fi
}
fetch "$TEMPLATE"
fetch "$APP.env.example"

# --- repositories folder
[ -d "$REPOS" ] || { say "Creating $REPOS"; mkdir -p "$REPOS"; }
REPOS=$(cd "$REPOS" && pwd)

# --- token (Podman secret)
if [ "$NEW_TOKEN" -eq 1 ]; then podman secret rm "$APP-token" >/dev/null 2>&1 || true; fi
if ! podman secret exists "$APP-token" 2>/dev/null; then
  say "Creating the access token (Podman secret $APP-token)"
  if command -v openssl >/dev/null; then TOKEN=$(openssl rand -hex 24)
  else TOKEN=$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n'); fi
  printf '%s' "$TOKEN" | podman secret create "$APP-token" - >/dev/null
  unset TOKEN
fi

# --- settings file: created once, then only the keys you pass are changed
mkdir -p "$CONF_DIR"
if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE"
  # The Podman secret holds the token; a placeholder token line here would override it.
  sed -E 's/^([A-Z_]*WEB_TOKEN=)/#\1/' "$TMP/$APP.env.example" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
# An empty or example token here would override the Podman secret.
sed -i -E 's/^([A-Z_]*WEB_TOKEN=(change-me)?)$/#\1/' "$ENV_FILE"
set_env() {   # set_env KEY VALUE: replace (or uncomment) the line, or append it
  local k=$1 v=$2
  if grep -qE "^#?${k}=" "$ENV_FILE"; then
    sed -i -E "0,/^#?${k}=.*/s||${k}=${v//|/\\|}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  fi
}
[ -n "$MODEL" ] && set_env "$MODEL_VAR" "$MODEL"

# --- the unit
bind="127.0.0.1:"; [ "$LAN" -eq 1 ] && bind=""
sed -i -E \
  -e "s|^Volume=[^:]*:/repos(:.*)?$|Volume=${REPOS}:/repos\\1|" \
  -e "s|^PublishPort=.*|PublishPort=${bind}${PORT}:${PORT_IN}|" \
  "$TMP/$TEMPLATE"
if [ -n "$OLLAMA" ]; then
  if grep -qE "^Environment=${OLLAMA_VAR}=" "$TMP/$TEMPLATE"; then
    sed -i -E "s|^Environment=${OLLAMA_VAR}=.*|Environment=${OLLAMA_VAR}=${OLLAMA}|" "$TMP/$TEMPLATE"
  else
    set_env "$OLLAMA_VAR" "$OLLAMA"
  fi
elif [ -f "$UNIT" ] && grep -qE "^Environment=${OLLAMA_VAR}=" "$UNIT"; then
  # keep the Ollama URL from an earlier install
  keep=$(grep -E "^Environment=${OLLAMA_VAR}=" "$UNIT" | head -1)
  sed -i -E "s|^Environment=${OLLAMA_VAR}=.*|${keep//|/\\|}|" "$TMP/$TEMPLATE"
fi
if [ "$MODE" = rootful ] && [ "$SET_USER" = yes ]; then
  owner=$(stat -c '%u' "$REPOS"); group=$(stat -c '%g' "$REPOS")
  if [ "$owner" = 0 ]; then
    echo "note: $REPOS is owned by root; $APP runs as uid 1000 and needs write access for fixes."
    echo "      chown it to the user that owns the repositories, or pass --repos."
    owner=1000; group=1000
  fi
  sed -i -E -e "s|^User=.*|User=${owner}|" -e "s|^Group=.*|Group=${group}|" "$TMP/$TEMPLATE"
fi

mkdir -p "$UNIT_DIR"
if [ -f "$UNIT" ] && ! cmp -s "$UNIT" "$TMP/$TEMPLATE"; then
  cp "$UNIT" "$UNIT.bak"
  say "Updating $UNIT (previous version in $APP.container.bak)"
else
  say "Writing $UNIT"
fi
install -m 644 "$TMP/$TEMPLATE" "$UNIT"

# Check the unit before handing it to systemd.
for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
  if [ -x "$q" ]; then
    args=(-dryrun); [ "$MODE" = rootless ] && args+=(-user)
    "$q" "${args[@]}" >/dev/null 2>"$TMP/quadlet.err" || { cat "$TMP/quadlet.err" >&2; die "Quadlet rejected $UNIT"; }
    break
  fi
done

say "Pulling the image (first time can take a minute)"
podman pull -q "$(sed -n 's/^Image=//p' "$UNIT")" >/dev/null || echo "note: pull failed; systemd will retry"

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" restart "$APP"
if [ "$MODE" = rootful ]; then
  systemctl enable --now podman-auto-update.timer >/dev/null 2>&1 || true
else
  systemctl --user enable --now podman-auto-update.timer >/dev/null 2>&1 || true
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
    echo "note: to keep $APP running after you log out and start it at boot, run once:"
    echo "      sudo loginctl enable-linger $USER"
  fi
fi

say "Waiting for $APP to answer on port $PORT"
ok=0
for _ in $(seq 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then ok=1; break; fi
  if ! "${SYSTEMCTL[@]}" is-active -q "$APP"; then break; fi
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  "${SYSTEMCTL[@]}" status "$APP" --no-pager -n 20 || true
  die "$APP did not start; see: journalctl ${JFLAG}-u $APP -n 50"
fi

TOKEN=$(podman secret inspect --showsecret --format '{{.SecretData}}' "$APP-token" 2>/dev/null || true)
host=localhost
if [ "$LAN" -eq 1 ]; then host=$(hostname -I 2>/dev/null | awk '{print $1}'); host=${host:-$(hostname)}; fi
echo
say "$APP is running ($MODE)"
echo "    open:     http://$host:$PORT/?token=${TOKEN:-<token>}"
echo "    repos:    $REPOS"
echo "    settings: $ENV_FILE   (then: ${SYSTEMCTL[*]} restart $APP)"
echo "    logs:     journalctl ${JFLAG}-u $APP -f"
[ "$LAN" -eq 1 ] && echo "    firewall: firewall-cmd --add-port=$PORT/tcp --permanent && firewall-cmd --reload  (if firewalld runs)"
exit 0
