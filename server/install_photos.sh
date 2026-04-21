#!/usr/bin/env bash
# Immich (photos module) install worker.
#
# Invoked detached by modules.spawn_lifecycle_script("photos", "install").
#
# What this does:
#   1. Verify Docker Compose is available.
#   2. Generate DB password + JWT secret + admin credentials (stable across
#      re-installs so user clients keep working).
#   3. Copy the compose template and .env into module-data/immich.
#   4. Pull images (slow first time — ~1.5 GB).
#   5. Write a systemd unit that runs `docker compose up -d`.
#   6. Start the unit, wait for the Immich API to respond.
#   7. Best-effort create admin via POST /api/auth/admin-sign-up.
#
# ML container is gated behind the `ml` compose profile. The install script
# defaults to enabling ML — the iOS ML toggle flips the `IMMICH_ENABLE_ML`
# value in .env and cycles the stack.

set -u
set -o pipefail

MODULE="photos"
IMMICH_VERSION="v1.140.0"  # pinned; bump only after local smoke testing
HOST_PORT=2283

STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/modules-state.json"
UGH_ROOT="/mnt/nvme/ughstorage"
DATA_DIR="$UGH_ROOT/module-data/immich"
UPLOAD_DIR="$UGH_ROOT/media/photos/upload"
DB_DIR="$DATA_DIR/pgdata"
MODEL_CACHE_DIR="$DATA_DIR/model-cache"
COMPOSE_FILE="$DATA_DIR/docker-compose.yml"
ENV_FILE="$DATA_DIR/.env"
UGH_ENV_FILE="$DATA_DIR/ughstorage.env"
UNIT_NAME="ugh-module-photos.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
HEALTH_URL="http://127.0.0.1:$HOST_PORT/api/server-info/ping"
HEALTH_TIMEOUT_SECS=180  # First-run DB init can take ~90s
SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_TEMPLATE="$SERVER_DIR/immich-compose.template.yml"

mkdir -p "$STATE_DIR" "$DATA_DIR" "$UPLOAD_DIR" "$DB_DIR" "$MODEL_CACHE_DIR"

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
    echo "[install_photos.sh] FAILED: $1" >&2
    exit 1
}

write_state "installing" "Checking Docker Compose" ""
if ! docker compose version >/dev/null 2>&1; then
    fail "Docker Compose not available — re-run setup.sh to install Docker"
fi

if [[ ! -f "$COMPOSE_TEMPLATE" ]]; then
    fail "Immich compose template missing at $COMPOSE_TEMPLATE"
fi

# --- credentials (stable across reinstalls) ---
write_state "installing" "Preparing credentials" ""
if [[ ! -f "$UGH_ENV_FILE" ]]; then
    DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    ADMIN_EMAIL="admin@ughstorage.local"
    ADMIN_NAME="UghStorage Admin"
    cat > "$UGH_ENV_FILE" <<UGH
IMMICH_VERSION=$IMMICH_VERSION
IMMICH_PORT=$HOST_PORT
DB_PASSWORD=$DB_PASSWORD
DB_USERNAME=immich
DB_DATABASE_NAME=immich
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_NAME=$ADMIN_NAME
ADMIN_PASSWORD=$ADMIN_PASSWORD
UGH
    chmod 600 "$UGH_ENV_FILE"
fi
# shellcheck disable=SC1090
source "$UGH_ENV_FILE"

# --- write compose file + Immich .env ---
write_state "installing" "Writing configuration" ""
cp "$COMPOSE_TEMPLATE" "$COMPOSE_FILE"

# The .env the compose file references. Separate from ughstorage.env so our
# admin creds don't leak into container env unnecessarily.
# IMMICH_ENABLE_ML is our own flag (iOS flips it); the install script default
# is "on" since that's the hero feature of this module.
IMMICH_ENABLE_ML="${IMMICH_ENABLE_ML:-true}"
cat > "$ENV_FILE" <<ENV
IMMICH_VERSION=$IMMICH_VERSION
IMMICH_PORT=$HOST_PORT
UPLOAD_LOCATION=$UPLOAD_DIR
DB_DATA_LOCATION=$DB_DIR
MODEL_CACHE_LOCATION=$MODEL_CACHE_DIR
DB_PASSWORD=$DB_PASSWORD
DB_USERNAME=immich
DB_DATABASE_NAME=immich
DB_HOSTNAME=database
DB_PORT=5432
REDIS_HOSTNAME=redis
IMMICH_ENABLE_ML=$IMMICH_ENABLE_ML
ENV
chmod 600 "$ENV_FILE"

# --- pull images (the slow part) ---
write_state "installing" "Pulling Immich images (this can take a few minutes)" ""
if ! (cd "$DATA_DIR" && docker compose --profile ml pull 2>&1); then
    fail "Failed to pull Immich Docker images"
fi

# --- clean up stale containers before writing unit ---
(cd "$DATA_DIR" && docker compose --profile ml down 2>/dev/null || true)

# --- systemd unit ---
write_state "installing" "Writing systemd unit" ""
# The unit's ExecStart/Stop pick the `ml` profile based on IMMICH_ENABLE_ML
# so toggling ML in iOS just rewrites .env and restarts the unit.
sudo /usr/bin/tee "$UNIT_PATH" > /dev/null <<UNIT
[Unit]
Description=Immich (UghStorage Photos Module)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/bin/bash -c 'if [ "\${IMMICH_ENABLE_ML}" = "true" ]; then exec docker compose --profile ml up -d; else exec docker compose up -d; fi'
ExecStop=/usr/bin/docker compose --profile ml down

[Install]
WantedBy=multi-user.target
UNIT

sudo /bin/systemctl daemon-reload
if ! sudo /bin/systemctl enable "$UNIT_NAME" 2>&1; then
    fail "Failed to enable $UNIT_NAME"
fi

write_state "installing" "Starting Immich stack" ""
if ! sudo /bin/systemctl restart "$UNIT_NAME" 2>&1; then
    fail "Failed to start $UNIT_NAME"
fi

# --- health wait ---
write_state "installing" "Waiting for Immich API (up to 3 minutes on first run)" ""
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECS ))
healthy=false
while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep 4
done

if ! $healthy; then
    fail "Immich did not respond on $HEALTH_URL within ${HEALTH_TIMEOUT_SECS}s. Check 'docker compose logs' under $DATA_DIR."
fi

# --- create admin (best-effort) ---
write_state "installing" "Creating admin account" ""
curl -fsS -X POST "http://127.0.0.1:$HOST_PORT/api/auth/admin-sign-up" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\",\"name\":\"$ADMIN_NAME\"}" \
    > /dev/null 2>&1 || true

write_state "idle" "" "Installed Immich $IMMICH_VERSION"
echo "[install_photos.sh] Install complete."
exit 0
