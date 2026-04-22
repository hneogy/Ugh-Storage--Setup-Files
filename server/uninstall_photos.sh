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

# Shared atomic+locked state writer (see _module_state.sh).
. "$(dirname "${BASH_SOURCE[0]}")/_module_state.sh"

write_state() {
    write_module_state "$MODULE" "$1" "${2:-}" "${3:-}"
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
