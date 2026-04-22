#!/usr/bin/env bash
# Shared helpers for module install/uninstall scripts.
# Sourced via: . "$(dirname "${BASH_SOURCE[0]}")/_module_state.sh"
#
# Provides:
#   write_module_state <module> <state> <step> <message>
#       Atomically merges {state, step, message, updated_at} into
#       $STATE_FILE under the given module key. flock'd against the same
#       lockfile that modules.py uses, so concurrent writers from the
#       FastAPI process and shell scripts can't clobber each other.
#       Falls back to a hand-rolled JSON if python3 is unavailable so
#       iOS still sees lifecycle progress.
#
# Required env: STATE_FILE (full path), STATE_DIR (parent).

if [[ -z "${STATE_DIR:-}" ]]; then
    STATE_DIR="${UGHSTORAGE_STATE_DIR:-/var/lib/ughstorage}"
fi
if [[ -z "${STATE_FILE:-}" ]]; then
    STATE_FILE="$STATE_DIR/modules-state.json"
fi
STATE_LOCK="${STATE_FILE%.json}.lock"

mkdir -p "$STATE_DIR"
# Touch the lockfile so flock(1) doesn't error before the first writer.
[[ -e "$STATE_LOCK" ]] || : > "$STATE_LOCK"

_write_module_state_locked() {
    local _module="$1" _state="$2" _step="${3:-}" _msg="${4:-}"
    local _tmp="${STATE_FILE}.tmp.$$"

    if MODULE="$_module" STATE_VALUE="$_state" STEP_VALUE="$_step" MSG_VALUE="$_msg" \
       STATE_FILE="$STATE_FILE" TMP_FILE="$_tmp" \
       python3 - <<'PY' 2>>"$STATE_DIR/install.log"
import json, os, sys
from datetime import datetime, timezone
state_file = os.environ["STATE_FILE"]
tmp = os.environ["TMP_FILE"]
try:
    with open(state_file) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
mod = os.environ["MODULE"]
data.setdefault(mod, {})
data[mod].update({
    "state": os.environ["STATE_VALUE"],
    "step": os.environ.get("STEP_VALUE") or None,
    "message": os.environ.get("MSG_VALUE") or None,
    "updated_at": datetime.now(timezone.utc).isoformat(),
})
with open(tmp, "w") as f:
    json.dump(data, f)
    f.flush()
    os.fsync(f.fileno())
sys.exit(0)
PY
    then
        mv -f "$_tmp" "$STATE_FILE"
        return 0
    fi

    # Fallback: hand-rolled single-module JSON. Loses other modules' entries
    # if the file already had them — only happens when python3 is broken,
    # which means the rest of the install is already in trouble.
    local _esc_msg _esc_step
    _esc_msg=$(printf '%s' "$_msg" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    _esc_step=$(printf '%s' "$_step" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    local _now
    _now=$(date -u +"%Y-%m-%dT%H:%M:%S.000000+00:00")
    cat > "$_tmp" <<EOF
{"$_module":{"state":"$_state","step":"$_esc_step","message":"$_esc_msg","updated_at":"$_now"}}
EOF
    mv -f "$_tmp" "$STATE_FILE"
}

write_module_state() {
    # Take an exclusive flock on $STATE_LOCK so only one writer runs at a time
    # — matches the fcntl.flock(LOCK_EX) on the same path in modules.py.
    # Subshell+redirect ensures fd 200 is opened to the lockfile only for
    # the duration of the locked critical section.
    (
        flock -x 200
        _write_module_state_locked "$@"
    ) 200>"$STATE_LOCK"
}
