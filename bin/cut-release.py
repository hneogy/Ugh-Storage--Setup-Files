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
  bin/cut-release.py 2.1.0 --public-repo /path/to/Ugh-Storage--Setup-Files

The signing key is read from ~/.ugh-signing.key (base64-encoded Ed25519 seed)
or the path in UGH_SIGNING_KEY_PATH. Generate it once with
bin/generate-signing-key.py.

Two-repo workflow:
  The private `ughstorage` repo is where dev happens. The public
  `Ugh-Storage--Setup-Files` repo is what user Pis clone + git-pull from.
  When --public-repo is set, the release is also published there:

    - server/, bin/, edge-functions/ are copied over (local artifacts
      like __pycache__ and venv/ stripped)
    - the same signed manifest + VERSION land on public's main
    - v<version> tag is created on public
    - public's `stable` branch is fast-forwarded so the manifest URL
      raw.githubusercontent.com/<you>/<public>/stable/server/manifest.json
      points at the new release

  The public-repo path can also come from env: UGH_PUBLIC_REPO_PATH.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
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


def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd or REPO_ROOT, text=True,
                            capture_output=capture, check=False)
    if check and result.returncode != 0:
        out = (result.stderr or result.stdout or "").strip()
        fail(f"command failed: {' '.join(cmd)}\n{out}")
    return result


def publish_to_public(public_repo: Path, version: str, stable_branch: str, push: bool) -> None:
    """Mirror the release to the public setup repo (Ugh-Storage--Setup-Files).

    Copies server/, bin/, edge-functions/ from this checkout (stripped of
    __pycache__ / venv), commits on the public repo's main, tags v<version>,
    fast-forwards <stable_branch>, pushes if push=True."""
    if not (public_repo / ".git").exists():
        fail(f"--public-repo {public_repo} is not a git checkout")

    # Refuse to publish to a dirty public checkout — we'd mix operator work
    # into the release commit otherwise.
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=public_repo, text=True, capture_output=True, check=False,
    ).stdout.strip()
    if dirty:
        fail(f"--public-repo {public_repo} has uncommitted changes; clean it first\n{dirty}")

    # Make sure we're on main + up to date so the push fast-forwards cleanly.
    run(["git", "checkout", "main"], cwd=public_repo)
    run(["git", "pull", "--ff-only", "origin", "main"], cwd=public_repo)

    # Mirror the three published top-level paths. Server/ is the one that
    # actually matters for the Pi; bin/ and edge-functions/ are published
    # for auditability (and so people can follow along with your release
    # process without access to the private repo).
    for path in ("server", "bin", "edge-functions"):
        src = REPO_ROOT / path
        dst = public_repo / path
        if not src.exists():
            continue
        # Nuke + replace — cleanest way to pick up deletions.
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(
            src, dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "venv", ".venv"),
        )

    run(["git", "add", "server", "bin", "edge-functions"], cwd=public_repo)

    # Nothing to commit? The release might have had no code changes (pure
    # manifest bump). Skip the commit but still tag.
    committed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=public_repo, check=False,
    ).returncode != 0
    if committed:
        run(["git", "commit", "-m", f"Release {version}"], cwd=public_repo)

    git_ref = f"v{version}"
    run(["git", "tag", "-f", git_ref], cwd=public_repo)

    if not push:
        print(f"  public: tag {git_ref} created locally at {public_repo}; not pushed (--no-push)")
        return

    run(["git", "push", "origin", "main", git_ref], cwd=public_repo)
    run(["git", "push", "origin", f"main:{stable_branch}", "--force-with-lease"], cwd=public_repo)
    print(f"  public: pushed {git_ref} + fast-forwarded {stable_branch}")


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
    ap.add_argument("--public-repo", default=os.environ.get("UGH_PUBLIC_REPO_PATH"),
                    help="path to a checkout of the public Ugh-Storage--Setup-Files "
                         "repo; release is also published there when set (or via "
                         "UGH_PUBLIC_REPO_PATH env)")
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
    # updates without needing a separate stable commit.
    run(["git", "push", "origin", f"main:{args.stable_branch}", "--force-with-lease"])

    print(f"Pushed {git_ref} + fast-forwarded {args.stable_branch}.")

    # Mirror to the public setup repo if configured. This is what user Pis
    # actually git-pull from during OTA, so skipping it means your release
    # never reaches end users.
    if args.public_repo:
        public_path = Path(args.public_repo).expanduser().resolve()
        print(f"\nPublishing to public repo at {public_path}")
        publish_to_public(public_path, args.version, args.stable_branch, push=True)
    else:
        print("\nNote: --public-repo not set. This release is only in the private repo.")
        print("      User Pis clone from the public repo and won't see this update.")
        print("      Re-run with --public-repo /path/to/Ugh-Storage--Setup-Files")
        print("      (or set UGH_PUBLIC_REPO_PATH) to publish to both.")

    print("\nDevices polling the manifest will pick this up within one check cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
