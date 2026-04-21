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
bin/cut-release.py 2.1.0 --notes "Bumps Navidrome to 0.55.0"
```

What happens:
1. Validates you're on `main` with a clean working tree.
2. Reads the pinned module versions from each `install_*.sh` so the manifest
   ships the versions the install scripts will actually install.
3. Writes the new version into `server/VERSION`.
4. Builds a signed manifest at `server/manifest.json`, using your private key
   from `~/.ugh-signing.key`.
5. Commits `VERSION + manifest.json`, tags the commit `v2.1.0`.
6. Pushes the tag and fast-forwards `origin/stable` to match, so the
   manifest URL (`https://raw.githubusercontent.com/<you>/ughstorage/stable/server/manifest.json`)
   picks up the new release immediately.

Useful flags:
- `--dry-run` — prints the would-be manifest without writing/committing.
- `--no-push` — makes the tag locally; you push manually.
- `--notes-file RELEASES.md` — inline vs. from a file.

Every Pi polling the configured manifest URL will see the new version on its
next check cycle (default 1 hour). From the iOS Settings → About row, the
user will see an "Update available" CTA and can tap Update to roll forward.
If anything goes wrong post-restart, `update.sh` rolls back to the previous
git SHA automatically.

## The signed-manifest contract

`update_manager._canonical_manifest_bytes` and the corresponding function in
`cut-release.py` must stay byte-identical. Both strip the `signature` field,
serialize the remaining JSON with `sort_keys=True, separators=(",",":")`, and
sign/verify those bytes. If you ever change this, update both sides together.

Signatures are Ed25519 (PyNaCl). Private key is 32 raw bytes,
base64-encoded at rest. Public key is the verify-half, also 32 bytes.
