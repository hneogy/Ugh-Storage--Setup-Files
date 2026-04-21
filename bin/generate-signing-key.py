#!/usr/bin/env python3
"""Generate a fresh Ed25519 signing keypair for UghStorage release manifests.

Run this ONCE per project. Writes:
  - ~/.ugh-signing.key (private key, 0600) — NEVER commit this
  - prints the public key to paste into server/update_manager.py
    (MANIFEST_PUBLIC_KEY_B64) and into Pi .env as UGHSTORAGE_UPDATE_PUBLIC_KEY

Key rotation: generate a new key, update the public key in all places, sign
the next release with the new private key. Old devices with the old public
key hardcoded in their current firmware will reject the new manifest — but
they'll still run the old firmware fine until the user SSHes in and updates.
"""

from __future__ import annotations

import base64
import os
import stat
import sys
from pathlib import Path

try:
    from nacl.signing import SigningKey
except ImportError:
    sys.stderr.write(
        "PyNaCl not installed. Run: pip install --user PyNaCl\n"
    )
    sys.exit(1)


DEFAULT_KEY_PATH = Path.home() / ".ugh-signing.key"


def main() -> int:
    key_path = Path(os.environ.get("UGH_SIGNING_KEY_PATH", DEFAULT_KEY_PATH))
    if key_path.exists():
        sys.stderr.write(
            f"refusing to overwrite existing key at {key_path}\n"
            f"move it aside first or set UGH_SIGNING_KEY_PATH to a different location\n"
        )
        return 1

    signing_key = SigningKey.generate()
    key_bytes = signing_key.encode()  # 32 raw bytes
    pubkey_bytes = signing_key.verify_key.encode()

    key_path.write_bytes(base64.b64encode(key_bytes))
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    print("Generated Ed25519 keypair.")
    print()
    print(f"  Private key written to: {key_path} (mode 0600)")
    print("    KEEP THIS FILE OFFLINE. Back it up somewhere safe (password manager,")
    print("    encrypted backup). If you lose it, you cannot sign new releases and")
    print("    will need to rotate to a new key across every device.")
    print()
    print("  Public key (base64, 32 bytes) — paste this in TWO places:")
    print()
    print("    1. server/update_manager.py:")
    print(f"       MANIFEST_PUBLIC_KEY_B64 = os.getenv(\"...\", \"{base64.b64encode(pubkey_bytes).decode()}\")")
    print()
    print("    2. Each Pi's .env (so the env var path also works):")
    print(f"       UGHSTORAGE_UPDATE_PUBLIC_KEY={base64.b64encode(pubkey_bytes).decode()}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
