# UghStorage release tooling

Two scripts live here:

## `generate-signing-key.py` (one-time, per project)

Generates an Ed25519 keypair. Writes the private half to `~/.ugh-signing.key`
(mode 0600) and prints the public half. Paste the public key into two places:

1. `server/update_manager.py` — the `MANIFEST_PUBLIC_KEY_B64` default string,
   so new builds of the server ship with the pubkey baked in.
2. Each Pi's `.env` — `UGHSTORAGE_UPDATE_PUBLIC_KEY=<pubkey>`. This is also
   where you'd set `UGHSTORAGE_UPDATE_REQUIRE_SIGNATURE=1` if you want the
   Pi to reject unsigned manifests outright.

**Back up the private key somewhere safe.** Losing it means you can't sign new
releases and have to rotate to a new key across every deployed Pi (which
means SSHing in until you cut a new firmware that ships the new pubkey, then
subsequent OTAs signed with the new private key are trusted).

## `cut-release.py` (every release)

```
bin/cut-release.py 2.1.0 --notes "Bumps Navidrome to 0.55.0" \
    --public-repo ~/src/Ugh-Storage--Setup-Files
```

What happens on the **private** `ughstorage` repo:
1. Validates you're on `main` with a clean working tree.
2. Reads pinned module versions from each `install_*.sh`.
3. Writes the new version into `server/VERSION`.
4. Builds a signed manifest at `server/manifest.json` using the private key
   from `~/.ugh-signing.key`.
5. Commits, tags `v2.1.0`, pushes `main` and fast-forwards `stable`.

Then on the **public** `Ugh-Storage--Setup-Files` repo (when `--public-repo`
is passed or `UGH_PUBLIC_REPO_PATH` is set):
6. Refuses if the public checkout is dirty.
7. Pulls the latest public `main` to avoid divergent histories.
8. Mirrors `server/`, `bin/`, `edge-functions/` from the private repo to
   the public one (strips `__pycache__`, `venv`).
9. Commits any changes on public's `main`, tags `v2.1.0`, fast-forwards
   public's `stable`, pushes.

The public repo is what end-user Pis clone from, and the manifest URL for
OTA should point at **the public repo's stable branch**:

```
https://raw.githubusercontent.com/hneogy/Ugh-Storage--Setup-Files/stable/server/manifest.json
```

Configure this on each Pi via `UGHSTORAGE_UPDATE_MANIFEST_URL` in `/home/pi/.../server/.env`.

Useful flags:
- `--dry-run` — prints the would-be manifest without writing/committing.
- `--no-push` — makes the tag locally; you push manually.
- `--notes-file RELEASES.md` — inline vs. from a file.
- `--public-repo PATH` — also publish to that public checkout. Skip if
  you're only iterating on the private repo.

Every Pi polling the configured manifest URL will see the new version on its
next check cycle. From iOS Settings → About, the user sees "Update available"
and can tap to roll forward. If health checks fail post-restart, `update.sh`
rolls back to the previous git SHA automatically.

## The signed-manifest contract

`update_manager._canonical_manifest_bytes` and the corresponding function in
`cut-release.py` must stay byte-identical. Both strip the `signature` field,
serialize the remaining JSON with `sort_keys=True, separators=(",",":")`, and
sign/verify those bytes. If you ever change this, update both sides together.

Signatures are Ed25519 (PyNaCl). Private key is 32 raw bytes,
base64-encoded at rest. Public key is the verify-half, also 32 bytes.
