#!/usr/bin/env python3
"""Cut a new UghStorage release.

What this does:
  1. Validates the working tree is clean and you're on `main`.
  2. Reads the current pinned module versions from install_*.sh files so the
     manifest ships the same versions the install scripts will actually use.
  3. Writes the new version string into server/VERSION.
  4. Generates a signed manifest at server/manifest.json.
  5. Commits VERSION + manifest.json, tags the commit, and (optionally) pushes
     the tag + a fast-forwarded `stable` branch to origin.
  6. Prints a summary and the next-step instructions.

Usage:
  bin/cut-release.py 2.1.0 --notes "Bumps Navidrome to 0.55.0"
  bin/cut-release.py 2.1.0 --notes-file RELEASES.md --dry-run
  bin/cut-release.py 2.1.0 --no-push    # local-only, no git push

The signing key is read from ~/.ugh-signing.key (base64-encoded Ed25519 seed)
or the path in UGH_SIGNING_KEY_PATH. Generate it once with
bin/generate-signing-key.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from nacl.signing import SigningKey
except ImportError:
    sys.stderr.write("PyNaCl not installed. Run: pip install --user PyNaCl\n")
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "server"
VERSION_FILE = SERVER_DIR / "VERSION"
MANIFEST_FILE = SERVER_DIR / "manifest.json"
DEFAULT_KEY_PATH = Path.home() / ".ugh-signing.key"

# Module install scripts + the regex that plucks the pinned version string.
MODULE_VERSION_SOURCES = {
    "music":  (SERVER_DIR / "install_music.sh",  re.compile(r'^\s*NAVIDROME_VERSION="([^"]+)"', re.M)),
    "photos": (SERVER_DIR / "install_photos.sh", re.compile(r'^\s*IMMICH_VERSION="([^"]+)"',    re.M)),
    "video":  (SERVER_DIR / "install_video.sh",  re.compile(r'^\s*JELLYFIN_VERSION="([^"]+)"',  re.M)),
}


def fail(msg: str) -> None:
    sys.stderr.write(f"cut-release: {msg}\n")
    sys.exit(1)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True,
                            capture_output=capture, check=False)
    if check and result.returncode != 0:
        out = (result.stderr or result.stdout or "").strip()
        fail(f"command failed: {' '.join(cmd)}\n{out}")
    return result


def ensure_clean_tree_on_main() -> None:
    status = run(["git", "status", "--porcelain"], capture=True).stdout.strip()
    if status:
        fail("working tree is dirty; commit or stash first\n" + status)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True).stdout.strip()
    if branch != "main":
        fail(f"refusing to cut a release from branch '{branch}' (expected 'main')")


def read_signing_key() -> SigningKey:
    path = Path(os.environ.get("UGH_SIGNING_KEY_PATH", DEFAULT_KEY_PATH))
    if not path.exists():
        fail(
            f"signing key not found at {path}\n"
            "Generate one with bin/generate-signing-key.py."
        )
    try:
        raw = base64.b64decode(path.read_bytes())
    except Exception as exc:
        fail(f"failed to decode signing key: {exc}")
    if len(raw) != 32:
        fail(f"signing key at {path} has unexpected length {len(raw)} (expected 32 bytes)")
    return SigningKey(raw)


def extract_module_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for module_id, (path, pattern) in MODULE_VERSION_SOURCES.items():
        if not path.exists():
            continue
        match = pattern.search(path.read_text())
        if match:
            out[module_id] = match.group(1)
        else:
            sys.stderr.write(
                f"warning: could not find pinned version in {path.name} — "
                f"module '{module_id}' will be omitted from manifest\n"
            )
    return out


def canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    """Must match update_manager._canonical_manifest_bytes exactly."""
    without_sig = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(without_sig, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Cut a new UghStorage release.")
    ap.add_argument("version", help="semver-ish version string, e.g. 2.1.0")
    ap.add_argument("--notes", default="", help="release notes (inline)")
    ap.add_argument("--notes-file", default=None, help="path to a file with release notes")
    ap.add_argument("--no-push", action="store_true",
                    help="skip pushing the tag / stable branch to origin")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; change nothing")
    ap.add_argument("--stable-branch", default="stable",
                    help="branch to fast-forward to the new tag (default: stable)")
    args = ap.parse_args()

    if not re.match(r"^\d+\.\d+\.\d+", args.version):
        fail(f"version '{args.version}' is not semver-ish")

    notes = args.notes
    if args.notes_file:
        notes_path = Path(args.notes_file)
        if not notes_path.exists():
            fail(f"notes file not found: {notes_path}")
        notes = notes_path.read_text()
    if not notes:
        notes = f"Release {args.version}"

    ensure_clean_tree_on_main()

    signing_key = read_signing_key()
    modules = extract_module_versions()
    git_ref = f"v{args.version}"

    manifest = {
        "latest_version": args.version,
        "git_ref": git_ref,
        "min_compatible_version": args.version,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "release_notes": notes,
        "modules": modules,
    }
    signature = signing_key.sign(canonical_manifest_bytes(manifest)).signature
    manifest["signature"] = base64.b64encode(signature).decode()

    # Also ship the public key in the manifest so operators can eyeball it
    # against the one burned into their Pis. The Pi never TRUSTS this value;
    # it only verifies against its own hardcoded pubkey. This is purely for
    # human inspection / rotation drills.
    manifest["signing_public_key"] = base64.b64encode(signing_key.verify_key.encode()).decode()

    print(f"=== Release {args.version} ===")
    print(f"  git_ref:         {git_ref}")
    print(f"  modules:         {modules}")
    print(f"  notes:           {notes.strip().splitlines()[0] if notes else ''}")
    print(f"  signature:       {manifest['signature'][:20]}…  (Ed25519)")
    print()

    if args.dry_run:
        print("--dry-run: not writing files, not committing, not pushing.")
        print(json.dumps(manifest, indent=2))
        return 0

    VERSION_FILE.write_text(args.version + "\n")
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n")

    run(["git", "add", str(VERSION_FILE.relative_to(REPO_ROOT)),
         str(MANIFEST_FILE.relative_to(REPO_ROOT))])
    run(["git", "commit", "-m", f"Release {args.version}"])
    run(["git", "tag", git_ref])

    if args.no_push:
        print("--no-push: tag created locally. To ship, run:")
        print(f"  git push origin main {git_ref}")
        print(f"  git push origin main:{args.stable_branch} --force-with-lease")
        return 0

    run(["git", "push", "origin", "main", git_ref])
    # Fast-forward the stable branch to this tag so the manifest URL
    # (https://raw.githubusercontent.com/<you>/ughstorage/stable/server/manifest.json)
    # updates without needing a separate stable commit.
    run(["git", "push", "origin", f"main:{args.stable_branch}", "--force-with-lease"])

    print(f"Pushed {git_ref} + fast-forwarded {args.stable_branch}.")
    print("Devices polling the manifest will pick this up within one check cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
