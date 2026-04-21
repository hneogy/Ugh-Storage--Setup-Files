"""Over-the-air update mechanism for the UghStorage server.

Design summary:
  - `VERSION` file at the repo root declares the current release string.
  - A remote manifest (configured via env) declares the latest published version
    and the git ref (tag) that corresponds to it.
  - Applying an update runs `server/update.sh` in a detached process so that
    restarting the ughstorage service mid-update does not kill the update.
  - The update script records its progress in a JSON file under
    /var/lib/ughstorage/ so the iOS app can poll status across restarts.
  - On health-check failure post-restart, the script rolls back to the git SHA
    that was active when the update started.

This module owns the *read* side (version, status, manifest check). The shell
script owns the *write* side (checkout, install, restart, rollback).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent
VERSION_FILE = SERVER_DIR / "VERSION"
UPDATE_SCRIPT = SERVER_DIR / "update.sh"

# State written by update.sh; readable here. Lives outside the repo so a
# `git checkout` mid-update cannot stomp on it.
STATE_DIR = Path(os.getenv("UGHSTORAGE_STATE_DIR", "/var/lib/ughstorage"))
STATE_FILE = STATE_DIR / "update-state.json"

# Configurable manifest URL. Empty = updates disabled (endpoints return a clear
# "not configured" response rather than crashing). Set this via the Pi's .env
# to the raw URL of a stable manifest.json (e.g. GitHub raw on a release branch).
MANIFEST_URL = os.getenv("UGHSTORAGE_UPDATE_MANIFEST_URL", "").strip()

# Ed25519 public key for verifying update manifests, in base64 (raw 32 bytes).
#
# Defense-in-depth: even if an attacker compromises the GitHub account hosting
# the manifest URL, they cannot produce a valid signature without the private
# key that lives only on the release-cutting machine. See bin/cut-release.py.
#
# Generate the keypair ONCE with bin/generate-signing-key.py, paste the public
# key here, keep the private key offline. DO NOT commit the private key.
#
# Until you've generated keys, leave this empty and manifests will be accepted
# on HTTPS-trust alone (a warning is logged on every check). Flip
# UGHSTORAGE_UPDATE_REQUIRE_SIGNATURE=1 in .env to make missing signatures fatal.
# Public key baked in at build time. Pis fetched via git pull inherit this,
# so fresh installs trust signed manifests without extra setup. Env var
# takes precedence so individual devices can be re-keyed if needed during a
# rotation (see bin/README.md for the rotation drill).
MANIFEST_PUBLIC_KEY_B64 = os.getenv(
    "UGHSTORAGE_UPDATE_PUBLIC_KEY",
    "zZfwKtXdrY0N88hoTp9cp9THoNTWwfjNML8qE9hlrB8=",
).strip()
REQUIRE_SIGNATURE = os.getenv("UGHSTORAGE_UPDATE_REQUIRE_SIGNATURE", "0") == "1"

# Valid states the script writes into update-state.json. iOS mirrors these.
UPDATE_STATES = {
    "idle",           # no update in flight
    "starting",       # script invoked, not yet fetching
    "fetching",       # git fetch in progress
    "installing",     # pip install in progress
    "restarting",     # systemctl restart issued
    "verifying",      # polling /health post-restart
    "complete",       # terminal success
    "rolled_back",    # health check failed; reverted to previous SHA
    "failed",         # terminal failure with no rollback (e.g. network gone)
}


def current_version() -> str:
    """Read the version string from the VERSION file. Falls back to 'unknown'."""
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


def current_git_sha() -> str | None:
    """Return the short git SHA of the running code, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def read_state() -> dict[str, Any]:
    """Read the current update-state JSON. Returns an idle sentinel if missing."""
    try:
        with STATE_FILE.open("r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state file is not an object")
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {"state": "idle", "updated_at": None, "message": None}


def _canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Produce the bytes the signature should cover. Matches bin/cut-release.py
    exactly — the signed payload is the manifest with its `signature` field
    removed, serialized as JSON with sorted keys and no whitespace."""
    without_sig = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(without_sig, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest_signature(manifest: dict[str, Any]) -> tuple[bool, str | None]:
    """Verify the Ed25519 signature on a manifest against the hardcoded pubkey.

    Returns (is_trusted, reason_if_not). An untrusted manifest can still be
    used if REQUIRE_SIGNATURE is False, but the reason is logged.
    """
    if not MANIFEST_PUBLIC_KEY_B64:
        return (False, "no public key configured")
    signature = manifest.get("signature")
    if not signature:
        return (False, "manifest has no signature field")
    try:
        pubkey = VerifyKey(base64.b64decode(MANIFEST_PUBLIC_KEY_B64))
    except Exception as exc:
        return (False, f"public key decode failed: {exc}")
    try:
        sig_bytes = base64.b64decode(signature)
    except Exception as exc:
        return (False, f"signature decode failed: {exc}")
    try:
        pubkey.verify(_canonical_manifest_bytes(manifest), sig_bytes)
        return (True, None)
    except BadSignatureError:
        return (False, "signature does not match")
    except Exception as exc:
        return (False, f"verify failed: {exc}")


async def fetch_manifest() -> dict[str, Any] | None:
    """Fetch, parse, and (when configured) verify the remote update manifest.
    Returns None if unconfigured, unreachable, or the signature-required
    mode rejects an unsigned/invalid manifest. Never raises — update checks
    should degrade gracefully."""
    if not MANIFEST_URL:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(MANIFEST_URL) as resp:
                if resp.status != 200:
                    logger.warning("Manifest fetch returned %d", resp.status)
                    return None
                manifest = await resp.json(content_type=None)
    except Exception:
        logger.exception("Manifest fetch failed")
        return None

    if not isinstance(manifest, dict):
        logger.warning("Manifest is not a JSON object")
        return None

    trusted, reason = verify_manifest_signature(manifest)
    if not trusted:
        if REQUIRE_SIGNATURE:
            logger.warning("Manifest rejected (REQUIRE_SIGNATURE=1): %s", reason)
            return None
        # Soft mode: accept on HTTPS trust but log so devops can spot drift.
        logger.info("Manifest accepted unsigned: %s", reason)

    return manifest


def _is_version_newer(latest: str, current: str) -> bool:
    """Semver-lite comparison. Assumes 'X.Y.Z' strings; trailing suffixes ignored.
    Returns False if either is malformed — safer than throwing during an
    update-check poll."""
    def parse(v: str) -> tuple[int, int, int] | None:
        try:
            parts = v.split(".")[:3]
            while len(parts) < 3:
                parts.append("0")
            return tuple(int("".join(ch for ch in p if ch.isdigit()) or "0") for p in parts)  # type: ignore[return-value]
        except Exception:
            return None
    a, b = parse(latest), parse(current)
    if a is None or b is None:
        return False
    return a > b


async def check_for_update() -> dict[str, Any]:
    """High-level "is there an update?" check suitable for a GET endpoint.
    Shape is stable — iOS decodes this directly.

    The `modules` map advertises what module versions THIS release ships
    (pinned in install_*.sh). iOS compares against each module's currently-
    running version to offer per-module "update available" CTAs. Missing
    from old manifests — clients treat that as "no module update info."
    """
    current = current_version()
    manifest = await fetch_manifest()
    if manifest is None:
        return {
            "current_version": current,
            "current_git_sha": current_git_sha(),
            "configured": bool(MANIFEST_URL),
            "available": False,
            "latest_version": None,
            "release_notes": None,
            "published_at": None,
            "signature_trusted": False,
            "modules": {},
        }
    latest = str(manifest.get("latest_version", current))
    trusted, _ = verify_manifest_signature(manifest)
    modules = manifest.get("modules") or {}
    if not isinstance(modules, dict):
        modules = {}
    return {
        "current_version": current,
        "current_git_sha": current_git_sha(),
        "configured": True,
        "available": _is_version_newer(latest, current),
        "latest_version": latest,
        "release_notes": manifest.get("release_notes"),
        "published_at": manifest.get("published_at"),
        "git_ref": manifest.get("git_ref"),
        "signature_trusted": trusted,
        "modules": {str(k): str(v) for k, v in modules.items()},
    }


async def trigger_update(target_git_ref: str) -> dict[str, Any]:
    """Kick off update.sh in a detached process. Returns immediately with the
    initial state; the iOS app polls `read_state()` via its endpoint to track
    progress.

    Safe to call if another update is in flight — the state file flags it and
    we return the in-flight status rather than spawning a second process."""
    if not UPDATE_SCRIPT.exists():
        return {"state": "failed", "message": f"update.sh not found at {UPDATE_SCRIPT}"}

    # Refuse to overlap updates.
    existing = read_state()
    if existing.get("state") in ("starting", "fetching", "installing", "restarting", "verifying"):
        return existing

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Write a "starting" state so iOS sees progress immediately, even before
    # the script's first write.
    initial = {
        "state": "starting",
        "target_git_ref": target_git_ref,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": "Update requested",
    }
    STATE_FILE.write_text(json.dumps(initial))

    # Detached: start_new_session so this survives the parent (uvicorn) restart.
    # stdout/stderr swallowed — the script logs to journalctl via systemd's
    # logger or to its own log file if run under systemd-run. For our first
    # cut we keep it simple: redirect to a rolling log under /var/lib.
    log_file = STATE_DIR / "update.log"
    await asyncio.create_subprocess_exec(
        "/bin/bash", str(UPDATE_SCRIPT), target_git_ref,
        cwd=str(SERVER_DIR),
        stdout=log_file.open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return initial
