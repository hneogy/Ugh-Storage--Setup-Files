#!/usr/bin/env bash
# Uninstall the video module. Stops Jellyfin, removes the systemd unit.
# Preserves:
#   - the user's video library at /mnt/nvme/ughstorage/media/video
#   - Jellyfin's config + cache under module-data/jellyfin
# so a reinstall keeps libraries, watch state, and user preferences.

set -u
set -o pipefail

MODULE="video"
CONTAINER_NAME="ugh-jellyfin"
UNIT_NAME="ugh-module-video.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/modules-state.json"

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

write_state "uninstalling" "Stopping service" ""
sudo /bin/systemctl stop "$UNIT_NAME" 2>&1 || true
sudo /bin/systemctl disable "$UNIT_NAME" 2>&1 || true

write_state "uninstalling" "Removing container" ""
docker stop "$CONTAINER_NAME" 2>&1 || true
docker rm "$CONTAINER_NAME" 2>&1 || true

write_state "uninstalling" "Removing systemd unit" ""
if [[ -f "$UNIT_PATH" ]]; then
    sudo /bin/rm "$UNIT_PATH" 2>&1 || true
    sudo /bin/systemctl daemon-reload 2>&1 || true
fi

# Intentionally preserved: media/video (the user's library) and
# module-data/jellyfin/{config,cache} (watch state, preferences, metadata).
write_state "idle" "" "Jellyfin uninstalled. Your video library and watch state were preserved."
echo "[uninstall_video.sh] Uninstall complete."
exit 0
