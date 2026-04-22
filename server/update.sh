#!/usr/bin/env bash
# UghStorage OTA update worker.
#
# Invoked by update_manager.trigger_update() as a detached process so it
# survives the parent uvicorn's restart. Writes progress to
# /var/lib/ughstorage/update-state.json so iOS can poll across restarts.
#
# Exits 0 on success, 1 after a rollback, 2 on misuse.

set -u
set -o pipefail

TARGET_REF="${1:-}"
if [[ -z "$TARGET_REF" ]]; then
    echo "usage: update.sh <git-ref>" >&2
    exit 2
fi

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/update-state.json"
HEALTH_URL="http://127.0.0.1:8000/health"
HEALTH_TIMEOUT_SECS=60
HEALTH_POLL_INTERVAL=3
VENV_PIP="$SERVER_DIR/venv/bin/pip"

mkdir -p "$STATE_DIR"

# write_state <state> <message>
# Atomically writes the JSON state file. Tries python3 first (proper JSON
# escaping for arbitrary message text); if python3 is broken or fails,
# falls back to a hand-rolled JSON write so iOS still sees update progress.
# Atomic via temp file + mv so a crashed half-write never lands.
write_state() {
    local _state="$1"
    local _msg="${2:-}"
    local _tmp="${STATE_FILE}.tmp.$$"

    if STATE_VALUE="$_state" STATE_MSG="$_msg" STATE_REF="$TARGET_REF" TMP_FILE="$_tmp" \
        python3 - <<'PY' 2>>"$STATE_DIR/update.log"
import json, os, sys
from datetime import datetime, timezone
data = {
    "state": os.environ["STATE_VALUE"],
    "target_git_ref": os.environ["STATE_REF"],
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "message": os.environ["STATE_MSG"],
}
with open(os.environ["TMP_FILE"], "w") as f:
    json.dump(data, f)
sys.exit(0)
PY
    then
        mv -f "$_tmp" "$STATE_FILE"
        return 0
    fi

    # Python failed — fall back to a hand-built JSON object. We escape only
    # the few characters that could break JSON in the message string.
    local _esc_msg
    _esc_msg=$(printf '%s' "$_msg" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a;N;$!ba;s/\n/\\n/g' -e 's/\r/\\r/g' -e 's/\t/\\t/g')
    local _esc_ref
    _esc_ref=$(printf '%s' "$TARGET_REF" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    local _now
    _now=$(date -u +"%Y-%m-%dT%H:%M:%S.000000+00:00")
    cat > "$_tmp" <<EOF
{"state":"$_state","target_git_ref":"$_esc_ref","updated_at":"$_now","message":"$_esc_msg"}
EOF
    mv -f "$_tmp" "$STATE_FILE"
}

rollback() {
    local reason="$1"
    echo "[update.sh] Rolling back: $reason" >&2
    write_state "installing" "Rolling back to $PREVIOUS_SHA — $reason"
    git -C "$SERVER_DIR" checkout --quiet "$PREVIOUS_SHA" 2>&1 || true
    "$VENV_PIP" install -q -r "$SERVER_DIR/requirements.txt" 2>&1 || true
    sudo /bin/systemctl restart ughstorage 2>&1 || true
    # Give the old version a chance to come back up before we declare done.
    sleep 5
    write_state "rolled_back" "Rolled back to $PREVIOUS_SHA. Reason: $reason"
    exit 1
}

fail() {
    write_state "failed" "$1"
    echo "[update.sh] FAILED: $1" >&2
    exit 1
}

echo "[update.sh] Target ref: $TARGET_REF"
write_state "fetching" "Fetching latest code"

# --- record current SHA for rollback ---
PREVIOUS_SHA="$(git -C "$SERVER_DIR" rev-parse HEAD 2>/dev/null || echo "")"
if [[ -z "$PREVIOUS_SHA" ]]; then
    fail "Not a git checkout; cannot update safely"
fi
echo "[update.sh] Previous SHA: $PREVIOUS_SHA"

# --- fetch new code ---
if ! git -C "$SERVER_DIR" fetch --tags --all --prune 2>&1; then
    fail "git fetch failed"
fi

if ! git -C "$SERVER_DIR" checkout --quiet "$TARGET_REF" 2>&1; then
    fail "git checkout $TARGET_REF failed"
fi

# --- install deps ---
write_state "installing" "Installing Python dependencies"
if [[ ! -x "$VENV_PIP" ]]; then
    fail "pip not found at $VENV_PIP"
fi
if ! "$VENV_PIP" install -q -r "$SERVER_DIR/requirements.txt" 2>&1; then
    rollback "pip install failed"
fi

# --- restart service ---
write_state "restarting" "Restarting UghStorage service"
# Uses the scoped passwordless sudo rule installed by setup.sh.
if ! sudo /bin/systemctl restart ughstorage 2>&1; then
    rollback "systemctl restart failed"
fi

# --- health check loop ---
write_state "verifying" "Verifying service health"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECS ))
healthy=false
while (( $(date +%s) < deadline )); do
    if curl -fsS --max-time 3 "$HEALTH_URL" > /dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep "$HEALTH_POLL_INTERVAL"
done

if ! $healthy; then
    rollback "Health check failed after ${HEALTH_TIMEOUT_SECS}s"
fi

write_state "complete" "Update to $TARGET_REF successful"
echo "[update.sh] Update complete."
exit 0
