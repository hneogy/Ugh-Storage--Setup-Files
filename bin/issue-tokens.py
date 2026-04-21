#!/usr/bin/env python3
"""Admin CLI for minting UghStorage provisioning tokens.

Calls the `issue-provisioning-tokens` Supabase edge function with a service-
role key, saves the minted tokens + QR-URL manifests to disk as CSV for a
label printer, and prints a human-readable summary to stdout.

Usage:
  export UGHSTORAGE_SERVICE_ROLE_KEY=eyJhbGc...   # keep this OFFLINE
  bin/issue-tokens.py --count 50 --sku ugh-pi5-16gb --batch-id 2026-05-run-a
  bin/issue-tokens.py --count 1 --dry-run          # show format without minting

Environment variables:
  UGHSTORAGE_SERVICE_ROLE_KEY   Supabase service-role key (required for real runs)
  UGHSTORAGE_SUPABASE_URL       Defaults to the project URL baked into the iOS app
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SUPABASE_URL = "https://ooadxfhisydhcgktaemt.supabase.co"
DEFAULT_CSV_DIR = Path.home() / "ugh-tokens"


def fail(msg: str) -> None:
    sys.stderr.write(f"issue-tokens: {msg}\n")
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint UghStorage provisioning tokens")
    ap.add_argument("--count", type=int, default=1, help="how many tokens to mint (1-1000)")
    ap.add_argument("--sku", default=None, help="SKU string (e.g. ugh-pi5-16gb)")
    ap.add_argument("--batch-id", default=None, help="arbitrary batch identifier")
    ap.add_argument("--expires-at", default=None, help="ISO-8601 expiry (e.g. for trial codes)")
    ap.add_argument("--notes", default=None, help="free-form operator notes")
    ap.add_argument("--csv-dir", default=str(DEFAULT_CSV_DIR),
                    help=f"where to save the CSV (default {DEFAULT_CSV_DIR})")
    ap.add_argument("--supabase-url", default=os.environ.get("UGHSTORAGE_SUPABASE_URL", DEFAULT_SUPABASE_URL))
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be minted; do not call the edge function")
    args = ap.parse_args()

    if not 1 <= args.count <= 1000:
        fail("--count must be between 1 and 1000")

    service_key = os.environ.get("UGHSTORAGE_SERVICE_ROLE_KEY", "").strip()
    if not service_key and not args.dry_run:
        fail("UGHSTORAGE_SERVICE_ROLE_KEY not set. Get it from Supabase dashboard → Project Settings → API.")

    if args.dry_run:
        print(f"DRY RUN: would mint {args.count} token(s)")
        print(f"  sku={args.sku}, batch_id={args.batch_id}, expires_at={args.expires_at}")
        return 0

    endpoint = f"{args.supabase_url}/functions/v1/issue-provisioning-tokens"
    payload = {
        "count": args.count,
        "sku": args.sku,
        "batch_id": args.batch_id,
        "expires_at": args.expires_at,
        "notes": args.notes,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            err = json.loads(body_bytes).get("error", body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            err = body_bytes.decode("utf-8", errors="replace")
        fail(f"edge function returned HTTP {exc.code}: {err}")
    except Exception as exc:
        fail(f"request failed: {exc}")

    tokens: list[str] = body.get("tokens", [])
    if not tokens:
        fail(f"edge function returned no tokens: {body}")

    # Write CSV — one row per token with a QR-friendly URL the iOS app can
    # parse. The URL scheme `ughprov://` is consumed by ActivateDeviceView.
    csv_dir = Path(args.csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = args.batch_id or args.sku or "tokens"
    # Sanitize tag for filesystem use.
    safe_tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)
    csv_path = csv_dir / f"ugh-{safe_tag}-{stamp}.csv"

    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "qr_url", "sticker_label", "sku", "batch_id", "minted_at"])
        for token in tokens:
            writer.writerow([
                token,
                f"ughprov://{token}",
                token,  # same string, printed monospace on the sticker
                args.sku or "",
                args.batch_id or "",
                datetime.now(timezone.utc).isoformat(),
            ])

    print(f"Minted {len(tokens)} token(s). Saved CSV to {csv_path}")
    print()
    print("First few:")
    for t in tokens[:5]:
        print(f"  {t}    (QR: ughprov://{t})")
    if len(tokens) > 5:
        print(f"  ... and {len(tokens) - 5} more in the CSV")

    return 0


if __name__ == "__main__":
    sys.exit(main())
