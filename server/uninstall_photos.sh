#!/usr/bin/env bash
# Uninstall the photos module. Stops the compose stack, removes the unit.
# Preserves:
#   - the user's upload library at /mnt/nvme/ughstorage/media/photos/upload
#   - Immich's Postgres data under module-data/immich/pgdata
#   - the ML model cache under module-data/immich/model-cache
# so a reinstall keeps people's accounts, photos, and analysis results.

set -u
set -o pipefail

MODULE="photos"
STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
STATE_FILE="$STATE_DIR/modules-state.json"
DATA_DIR="/mnt/nvme/ughstorage/module-data/immich"
UNIT_NAME="ugh-module-photos.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

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

write_state "uninstalling" "Stopping containers" ""
if [[ -f "$DATA_DIR/docker-compose.yml" ]]; then
    (cd "$DATA_DIR" && docker compose --profile ml down 2>&1 || true)
fi

write_state "uninstalling" "Removing systemd unit" ""
if [[ -f "$UNIT_PATH" ]]; then
    sudo /bin/rm "$UNIT_PATH" 2>&1 || true
    sudo /bin/systemctl daemon-reload 2>&1 || true
fi

# Intentionally NOT deleted: module-data/immich/{pgdata,model-cache} and
# media/photos/upload. These preserve the user's library and Immich state
# so reinstalling brings them back to exactly where they were.
write_state "idle" "" "Immich uninstalled. Your photo library was preserved."
echo "[uninstall_photos.sh] Uninstall complete."
exit 0
