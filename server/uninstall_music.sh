#!/usr/bin/env bash
# Uninstall the music module. Stops the systemd unit, removes the container,
# and deletes the unit file. Deliberately does NOT touch:
#   - the user's music library at /mnt/nvme/ughstorage/media/music
#   - the Navidrome data directory at /mnt/nvme/ughstorage/module-data/music
#     (so re-install keeps listen history, ratings, playlists)
#
# Matches install_music.sh's state-write pattern so iOS can poll progress.

set -u
set -o pipefail

MODULE="music"
CONTAINER_NAME="ugh-navidrome"
UNIT_NAME="ugh-module-music.service"
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

# We intentionally keep module-data/music and media/music so the user's
# library survives an uninstall/reinstall cycle. If they want to wipe that,
# they can reset the whole device.
write_state "idle" "" "Navidrome uninstalled. Music files at media/music were preserved."
echo "[uninstall_music.sh] Uninstall complete."
exit 0
