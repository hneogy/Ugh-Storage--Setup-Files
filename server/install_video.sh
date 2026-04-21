#!/usr/bin/env bash
# Jellyfin (video module) install worker.
#
# Invoked detached by modules.spawn_lifecycle_script("video", "install").
#
# What this does:
#   1. Pull the pinned Jellyfin Docker image.
#   2. Generate stable-across-reinstalls admin credentials.
#   3. Write a systemd unit that runs `docker run` with the Jellyfin image.
#   4. Start the unit, wait for Jellyfin's API to respond.
#   5. Automate the first-run wizard (language, admin user, disable remote
#      access) via the /Startup/* API. Best-effort — if the wizard is already
#      complete (re-install) or the endpoints have shifted upstream, the user
#      can complete it manually in the Jellyfin web UI.
#
# Storage layout:
#   - /mnt/nvme/ughstorage/media/video → mounted at /media/video (read-only)
#     so Jellyfin can't modify user files. User creates libraries inside
#     Jellyfin pointing at subfolders of /media/video.
#   - /mnt/nvme/ughstorage/module-data/jellyfin/config → container /config
#   - /mnt/nvme/ughstorage/module-data/jellyfin/cache  → container /cache

set -u
set -o pipefail

MODULE="video"
JELLYFIN_VERSION="10.9.11"  # pinned; bump only after local smoke testing
CONTAINER_NAME="ugh-jellyfin"
HOST_PORT=8096

STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/modules-state.json"
UGH_ROOT="/mnt/nvme/ughstorage"
MEDIA_DIR="$UGH_ROOT/media/video"
DATA_DIR="$UGH_ROOT/module-data/jellyfin"
CONFIG_DIR="$DATA_DIR/config"
CACHE_DIR="$DATA_DIR/cache"
ENV_FILE="$DATA_DIR/.env"
UNIT_NAME="ugh-module-video.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
PING_URL="http://127.0.0.1:$HOST_PORT/System/Info/Public"
HEALTH_TIMEOUT_SECS=90

mkdir -p "$STATE_DIR" "$CONFIG_DIR" "$CACHE_DIR" "$MEDIA_DIR"

write_state() {
    MODULE="$MODULE" STATE_VALUE="$1" STEP_VALUE="${2:-}" MSG_VALUE="${3:-}" \
        STATE_FILE="$STATE_FILE" python3 - <<'PY'
import json, os
from datetime import datetime, timezone
try:
    with open(os.environ["STATE_FILE"]) as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
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
    echo "[install_video.sh] FAILED: $1" >&2
    exit 1
}

write_state "installing" "Pulling Jellyfin image" ""
if ! docker pull "jellyfin/jellyfin:$JELLYFIN_VERSION" 2>&1; then
    fail "Failed to pull Docker image"
fi

# --- credentials (stable across reinstalls so client configs keep working) ---
write_state "installing" "Preparing credentials" ""
if ! grep -q "^JELLYFIN_ADMIN_PASSWORD=" "$ENV_FILE" 2>/dev/null; then
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    cat > "$ENV_FILE" <<ENV
JELLYFIN_ADMIN_USER=ughadmin
JELLYFIN_ADMIN_PASSWORD=$ADMIN_PASSWORD
JELLYFIN_VERSION=$JELLYFIN_VERSION
ENV
    chmod 600 "$ENV_FILE"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

# Clean up any stale container from a half-finished previous attempt.
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true

write_state "installing" "Writing systemd unit" ""
# Bind Jellyfin to 0.0.0.0 for LAN access (matches Sprint (c) LAN-only stance
# for media modules). Volume mounts: config RW, cache RW, media read-only so
# Jellyfin can't modify user files — read-only protects against library edits
# from inside Jellyfin clobbering a user's storage-module organization.
sudo /usr/bin/tee "$UNIT_PATH" > /dev/null <<UNIT
[Unit]
Description=Jellyfin (UghStorage Video Module)
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
    -p $HOST_PORT:8096 \\
    -v $CONFIG_DIR:/config \\
    -v $CACHE_DIR:/cache \\
    -v $MEDIA_DIR:/media/video:ro \\
    jellyfin/jellyfin:$JELLYFIN_VERSION
ExecStop=/usr/bin/docker stop $CONTAINER_NAME

[Install]
WantedBy=multi-user.target
UNIT

sudo /bin/systemctl daemon-reload
if ! sudo /bin/systemctl enable "$UNIT_NAME" 2>&1; then
    fail "Failed to enable $UNIT_NAME"
fi

write_state "installing" "Starting Jellyfin" ""
if ! sudo /bin/systemctl restart "$UNIT_NAME" 2>&1; then
    fail "Failed to start $UNIT_NAME"
fi

# --- health wait ---
write_state "installing" "Waiting for Jellyfin API" ""
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECS ))
ready=false
while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$PING_URL" > /dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 3
done

if ! $ready; then
    fail "Jellyfin did not respond on $PING_URL within ${HEALTH_TIMEOUT_SECS}s"
fi

# --- first-run wizard automation (best-effort) ---
# Jellyfin's first-run wizard is browser-based by default, but every step is
# also a POST to /Startup/*. We automate so the user doesn't have to visit
# the web UI before their iOS app can connect. If any step fails (e.g. the
# wizard is already complete from a prior install, or the endpoints shifted
# in a version bump), we log it and continue — the user can complete the
# wizard manually in their browser.
write_state "installing" "Running first-run setup" ""

ADMIN_USER="$(grep '^JELLYFIN_ADMIN_USER=' "$ENV_FILE" | cut -d= -f2-)"
ADMIN_PASSWORD="$(grep '^JELLYFIN_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"

# Jellyfin accepts these without auth during first-run (the wizard endpoint
# becomes inert after completion). Attempt all of them; ignore failures.
STARTUP_HEADERS=(-H "Content-Type: application/json" -H "X-Emby-Token: none")

# 1. Language
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/Startup/Configuration" \
    "${STARTUP_HEADERS[@]}" \
    -d '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}' \
    > /dev/null 2>&1 || true

# 2. Admin user
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/Startup/User" \
    "${STARTUP_HEADERS[@]}" \
    -d "{\"Name\":\"$ADMIN_USER\",\"Password\":\"$ADMIN_PASSWORD\"}" \
    > /dev/null 2>&1 || true

# 3. Remote access (disable — we handle reach via LAN in Sprint (e); Tailscale
#    comes later). This also disables Jellyfin's own UPnP mappings.
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/Startup/RemoteAccess" \
    "${STARTUP_HEADERS[@]}" \
    -d '{"EnableRemoteAccess":false,"EnableAutomaticPortMapping":false}' \
    > /dev/null 2>&1 || true

# 4. Mark wizard complete.
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/Startup/Complete" \
    "${STARTUP_HEADERS[@]}" \
    > /dev/null 2>&1 || true

write_state "idle" "" "Installed Jellyfin $JELLYFIN_VERSION"
echo "[install_video.sh] Install complete."
exit 0
