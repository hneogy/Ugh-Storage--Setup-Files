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

# Shared atomic+locked state writer (see _module_state.sh).
. "$(dirname "${BASH_SOURCE[0]}")/_module_state.sh"

write_state() {
    write_module_state "$MODULE" "$1" "${2:-}" "${3:-}"
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
