#!/usr/bin/env bash
# Navidrome (music module) install worker.
#
# Invoked detached by modules.spawn_lifecycle_script("music", "install"). Writes
# progress to /var/lib/ughstorage/modules-state.json so the FastAPI endpoint
# can stream status to the iOS app across restarts.
#
# What this does:
#   1. Pull the pinned Navidrome Docker image.
#   2. Generate admin credentials (written to module-data/music/.env).
#   3. Write a systemd unit that runs `docker run ...` for Navidrome.
#   4. Start the unit, wait for /ping to succeed, update state.
#
# Idempotent-ish: safe to re-run if the previous attempt half-finished. A
# stale container with the same name is stopped + removed first.

set -u
set -o pipefail

MODULE="music"
NAVIDROME_VERSION="0.54.1"  # pinned; bump only after local smoke testing
CONTAINER_NAME="ugh-navidrome"
HOST_PORT=4533
INTERNAL_PORT=4533

STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/modules-state.json"
UGH_ROOT="/mnt/nvme/ughstorage"
MEDIA_DIR="$UGH_ROOT/media/music"
DATA_DIR="$UGH_ROOT/module-data/music"
ENV_FILE="$DATA_DIR/.env"
UNIT_NAME="ugh-module-music.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
HEALTH_URL="http://127.0.0.1:$HOST_PORT/ping"
HEALTH_TIMEOUT_SECS=90

mkdir -p "$STATE_DIR" "$DATA_DIR" "$MEDIA_DIR"

# write_state <state> <step> <message>
write_state() {
    MODULE="$MODULE" STATE_VALUE="$1" STEP_VALUE="${2:-}" MSG_VALUE="${3:-}" \
        STATE_FILE="$STATE_FILE" python3 - <<'PY'
import json, os
from datetime import datetime, timezone
try:
    with open(os.environ["STATE_FILE"]) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError:
    data = {}
data.setdefault(os.environ["MODULE"], {})
data[os.environ["MODULE"]].update({
    "state": os.environ["STATE_VALUE"],
    "step": os.environ.get("STEP_VALUE") or None,
    "message": os.environ.get("MSG_VALUE") or None,
    "updated_at": datetime.now(timezone.utc).isoformat(),
})
with open(os.environ["STATE_FILE"], "w") as f:
    json.dump(data, f)
PY
}

fail() {
    write_state "failed" "" "$1"
    echo "[install_music.sh] FAILED: $1" >&2
    exit 1
}

write_state "installing" "Pulling Navidrome image" ""
echo "[install_music.sh] docker pull deluan/navidrome:$NAVIDROME_VERSION"
if ! docker pull "deluan/navidrome:$NAVIDROME_VERSION" 2>&1; then
    fail "Failed to pull Docker image"
fi

write_state "installing" "Preparing credentials" ""
# Generate admin password only if not already present (allows re-runs to keep
# creds stable so the user's music clients don't break after re-install).
if ! grep -q "^NAVIDROME_ADMIN_PASSWORD=" "$ENV_FILE" 2>/dev/null; then
    # 32 hex chars — easy to type if the user ever has to copy it manually.
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    cat > "$ENV_FILE" <<ENV
NAVIDROME_ADMIN_USER=ughadmin
NAVIDROME_ADMIN_PASSWORD=$ADMIN_PASSWORD
NAVIDROME_VERSION=$NAVIDROME_VERSION
ENV
    chmod 600 "$ENV_FILE"
fi

# Stop+remove any stale container so re-runs don't hit "name in use".
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true

write_state "installing" "Writing systemd unit" ""
# systemd unit wraps `docker run` with proper lifecycle management. Using
# --env-file so the admin password never lives in the unit file. Binding to
# 0.0.0.0 because Sprint (c) is LAN-only for media modules; later we'll add
# a reverse-proxy through the tunnel for remote access.
sudo /usr/bin/tee "$UNIT_PATH" > /dev/null <<UNIT
[Unit]
Description=Navidrome (UghStorage Music Module)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker stop $CONTAINER_NAME
ExecStartPre=-/usr/bin/docker rm $CONTAINER_NAME
ExecStart=/usr/bin/docker run --rm --name $CONTAINER_NAME \\
    -p $HOST_PORT:$INTERNAL_PORT \\
    --env-file $ENV_FILE \\
    -e ND_MUSICFOLDER=/music \\
    -e ND_DATAFOLDER=/data \\
    -e ND_LOGLEVEL=info \\
    -e ND_PORT=$INTERNAL_PORT \\
    -v $MEDIA_DIR:/music:ro \\
    -v $DATA_DIR:/data \\
    deluan/navidrome:$NAVIDROME_VERSION
ExecStop=/usr/bin/docker stop $CONTAINER_NAME

[Install]
WantedBy=multi-user.target
UNIT

sudo /bin/systemctl daemon-reload
if ! sudo /bin/systemctl enable "$UNIT_NAME" 2>&1; then
    fail "Failed to enable $UNIT_NAME"
fi

write_state "installing" "Starting Navidrome" ""
if ! sudo /bin/systemctl restart "$UNIT_NAME" 2>&1; then
    fail "Failed to start $UNIT_NAME"
fi

# --- health wait ---
write_state "installing" "Waiting for health check" ""
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECS ))
healthy=false
while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep 3
done

if ! $healthy; then
    fail "Navidrome did not respond on $HEALTH_URL within ${HEALTH_TIMEOUT_SECS}s"
fi

# --- create admin (best-effort) ---
# Navidrome's first-login promotes the user to admin. We hit the REST API so
# the iOS app can hand the creds straight to a Subsonic client. If this fails
# (e.g. admin already exists from a prior install), we don't treat it as fatal
# — the creds in $ENV_FILE still work since the user's first login is admin.
write_state "installing" "Creating admin account" ""
ADMIN_USER="$(grep '^NAVIDROME_ADMIN_USER=' "$ENV_FILE" | cut -d= -f2-)"
ADMIN_PASSWORD="$(grep '^NAVIDROME_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/auth/createAdmin" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" \
    > /dev/null 2>&1 || true

write_state "idle" "" "Installed Navidrome $NAVIDROME_VERSION"
echo "[install_music.sh] Install complete."
exit 0
