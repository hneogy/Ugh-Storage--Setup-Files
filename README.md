# Ugh! Storage

**Your own private cloud. On hardware you own. With no monthly bill.**

Turn a Raspberry Pi 5 into a personal cloud that backs up your photos,
streams your music and movies, and holds everything else you'd normally
pay iCloud, Google Photos, or Dropbox to hold. Your files stay on your
NVMe drive at home. The iOS app reaches them over a secure tunnel so
you can still use them from anywhere.

| | iCloud 2TB | Google One 2TB | Ugh! Storage 1TB |
|---|---|---|---|
| Year 1 | $120 | $100 | ~$130 (hardware) |
| Year 5 | $600 | $500 | ~$190 |
| Who sees your files? | Apple | Google | **Nobody** |
| Expandable? | No | No | Swap the SSD |

---

## Table of contents

- [What you get](#what-you-get)
- [How it all fits together](#how-it-all-fits-together)
  - [Everyday use](#everyday-use)
  - [First-time setup](#first-time-setup)
  - [Services running on the Pi](#services-running-on-the-pi)
  - [How files are laid out on disk](#how-files-are-laid-out-on-disk)
- [Trust, privacy, and security](#trust-privacy-and-security)
  - [What's protected](#whats-protected)
  - [What's **not** protected](#whats-not-protected-honest-version)
  - [How updates are trusted](#how-updates-are-trusted-the-signing-story)
- [Is this for me?](#is-this-for-me)
- [What you'll need](#what-youll-need)
- [Setup guide (30 minutes, one time)](#setup-guide)
- [Activation codes](#activation-codes-why-you-need-one)
- [The modules explained](#the-modules-explained)
- [Updates](#updates-how-new-versions-reach-your-pi)
- [Managing your device](#managing-your-device)
- [Troubleshooting](#troubleshooting)
- [FAQ](#frequently-asked-questions)
- [Licenses of components we bundle](#licenses-of-components-we-bundle)
- [Contributing and reporting issues](#contributing)

---

## What you get

Ugh! Storage is **four products in one device**, each individually
installable from the iOS app:

### Storage (always on)
- Upload / download / search / share files from your iPhone or Mac
- Photo & video backup (auto, Wi-Fi-only optional)
- On-device photo search: OCR of text in images, face clustering,
  smart albums — every bit of this runs **on your iPhone**, not in any
  cloud
- Client-side end-to-end encryption (opt-in passphrase)
- Trash with retention, favorites, public share links with QR codes
- File export (zip of everything + metadata manifest)

### Music (Navidrome, optional)
- Subsonic-compatible music server for your ripped / owned music library
- Point any Subsonic client (Substreamer, Amperfy, play:Sub, etc.) at
  your Pi and stream

### Photos (Immich, optional)
- Full Immich installation — auto photo backup from iPhone, face
  recognition, visual search, albums, shared albums
- Uses the official Immich iOS app once installed

### Video (Jellyfin, optional)
- Jellyfin media server for movies, shows, music videos
- Use the Jellyfin app or Infuse on iPhone/Apple TV
- Honest caveat: the Pi can only transcode a little — for best playback
  use clients that direct-play (Infuse shines here)

All four modules share the same NVMe drive, the same iOS app for
management, and the same updater. You can enable / disable any of them
from the iOS app without SSHing in.

---

## How it all fits together

### Everyday use

Once a Pi is set up, your iPhone reaches it through a Cloudflare
Tunnel — no ports open on your home router, no files stored in any
cloud.

```
                          ┌─────────────────────────────┐
                          │     cloudflare network      │
                          │                             │
 ┌──────────┐    HTTPS    │   ┌───────────────────┐     │    tunnel     ┌────────────────┐
 │  iPhone  │────────────▶│   │  <subdomain>      │─────│──────────────▶│   your Pi      │
 │   app    │◀────────────│   │  .ughstorage.com  │     │               │   (home)       │
 └──────────┘             │   └───────────────────┘     │               │                │
    anywhere              │                             │               │   FastAPI      │
                          │   tunnel runs outbound      │               │   on :8000     │
                          │   from Pi; nothing exposed  │               └────────────────┘
                          │   on your home network      │
                          └─────────────────────────────┘
```

Your iPhone talks to `https://<your-subdomain>.ughstorage.com`. That
domain is a Cloudflare Tunnel that terminates inside your Pi. Your
files flow through Cloudflare's network as encrypted bytes — Cloudflare
can't read them, and they never hit any of our servers.

### First-time setup

The Pi has no internet until you set it up. Your iPhone configures the
Wi-Fi, registers the device with our cloud, and provisions the tunnel
— all over **Bluetooth**:

```
 ┌──────────┐   Bluetooth   ┌────────────┐   Wi-Fi    ┌──────────┐
 │  iPhone  │──────────────▶│    Pi      │───────────▶│  router  │
 │   app    │   LE GATT     │  BLE server│   config   │          │
 └──────────┘               └─────┬──────┘            └──────────┘
    1. find Pi                    │
    2. send Wi-Fi creds           │
    3. send activation code       │
    4. send user auth token       │
                                  ▼
                          ┌────────────────┐
                          │ register-device│  ──▶  validates activation
                          │ Supabase edge  │        code, creates tunnel,
                          │   function     │        stores device row
                          └────────┬───────┘
                                   ▼
                          ┌────────────────────┐
                          │ Cloudflare Tunnel  │
                          │ auto-provisioned   │
                          │ <subdomain>.       │
                          │   ughstorage.com   │
                          └────────────────────┘
```

You don't SSH in to do any of this. The iOS app walks you through
seven short screens: find the Pi → pick your Wi-Fi → enter its
password → name your device → enter the [activation
code](#activation-codes-why-you-need-one) → register → done.

### Services running on the Pi

Systemd starts these on boot. You never have to touch them.

```
┌────────────────────────────────────────────────────────────────────┐
│                      Raspberry Pi 5                                │
│                                                                    │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────────┐     │
│  │  ughstorage     │  │  ughstorage  │  │  cloudflared      │     │
│  │  (FastAPI)      │  │  -ble        │  │  (tunnel)         │     │
│  │                 │  │  Bluetooth   │  │  Outbound-only    │     │
│  │  Storage + OTA  │  │  provisioning│  │  HTTPS to         │     │
│  │  Module mgmt    │  │              │  │  Cloudflare       │     │
│  │  on port 8000   │  │              │  │                   │     │
│  └─────────────────┘  └──────────────┘  └───────────────────┘     │
│                                                                    │
│  Optional media modules (enable/disable from iOS app):             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐     │
│  │ ugh-module-     │  │ ugh-module-     │  │ ugh-module-    │     │
│  │   music         │  │   photos        │  │   video        │     │
│  │ Navidrome       │  │ Immich          │  │ Jellyfin       │     │
│  │ (Docker)        │  │ (Docker Compose)│  │ (Docker)       │     │
│  │ :4533           │  │ :2283           │  │ :8096          │     │
│  └─────────────────┘  └─────────────────┘  └────────────────┘     │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  /mnt/nvme (your NVMe SSD)                                   │  │
│  │    storage/                 ← your files                     │  │
│  │    thumbnails/              ← auto-generated                 │  │
│  │    hls/                     ← video stream segments          │  │
│  │    ughstorage/                                               │  │
│  │      media/music            ← music library for Navidrome    │  │
│  │      media/photos/upload    ← photos backed up to Immich     │  │
│  │      media/video            ← movies / shows for Jellyfin    │  │
│  │      module-data/...        ← each module's internal state   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

### How files are laid out on disk

Everything lives under `/mnt/nvme/` on your NVMe drive. Predictable
paths, no surprises:

| Path | What's in it | Survives a module uninstall? |
|---|---|---|
| `/mnt/nvme/storage/` | Your uploaded files from the Ugh! Storage app | Yes |
| `/mnt/nvme/thumbnails/` | Auto-generated image thumbnails | Yes |
| `/mnt/nvme/hls/` | HLS segments for video streaming | Yes |
| `/mnt/nvme/ughstorage/media/music/` | Music library for Navidrome | Yes |
| `/mnt/nvme/ughstorage/media/photos/upload/` | Immich uploads | Yes |
| `/mnt/nvme/ughstorage/media/video/` | Jellyfin library | Yes |
| `/mnt/nvme/ughstorage/module-data/music/` | Navidrome internal state | Yes |
| `/mnt/nvme/ughstorage/module-data/immich/` | Immich Postgres + model cache | Yes |
| `/mnt/nvme/ughstorage/module-data/jellyfin/` | Jellyfin config + watch history | Yes |

**Factory reset** from the iOS app wipes the row in our cloud, stops
all module services, but **preserves your library files on disk** until
you explicitly reformat. Reinstalling a module picks up exactly where
you left off.

---

## Trust, privacy, and security

We're not asking you to trust a "we'd never look at your files"
promise. The architecture makes it **impossible** for us to look. Here
are the exact mechanics:

### What's protected

- **Files never leave your hardware.** They're stored on your NVMe,
  served from your Pi, and pass through Cloudflare as already-encrypted
  TLS payloads that Cloudflare can't decrypt (this is how any HTTPS
  origin behind a Cloudflare Tunnel works).
- **Zero inbound ports on your router.** The Pi opens an outbound
  tunnel to Cloudflare. Nothing on your home network accepts incoming
  connections from the internet.
- **Per-device authentication token** signed with a per-device secret
  generated at registration. A compromise of one Pi doesn't let an
  attacker touch another.
- **Client-side end-to-end encryption (optional).** Turn on a
  passphrase and files are AES-256-GCM encrypted *on your iPhone*
  before they're sent to the Pi. Even if someone gets physical access
  to the Pi, they see ciphertext.
- **Biometric lock** on the iOS app. Face ID or Touch ID gate the app
  even if your iPhone is unlocked.
- **Two-factor authentication** via TOTP for your Ugh! Storage account.
- **On-device AI.** All the "smart" features (OCR, face clustering,
  speech-to-text, entity extraction, Apple Foundation Models) run on
  your iPhone using Apple's on-device frameworks. None of your content
  is sent to any AI provider, not even ours.

### What's **not** protected (honest version)

- **Our cloud backend knows your device exists.** We run a Supabase
  project that stores your email, the fact that you own a device, the
  device's Cloudflare tunnel URL, storage totals for dashboard display,
  and online/offline status. We do not store file metadata, filenames,
  photos, or thumbnails.
- **Cloudflare sees traffic shape.** They can see *that* your phone
  connects to your Pi and how much data moves. They cannot see file
  contents (TLS inside the tunnel).
- **Someone with physical access to your Pi** could remove the NVMe
  drive and read it (unless you enabled client-side encryption). Treat
  the Pi like any other computer in your home.
- **We can't recover your passphrase.** If you enable client-side
  encryption and forget the passphrase, the encrypted files are
  unrecoverable. That's the point.

### How updates are trusted (the signing story)

When the Ugh! Storage server on your Pi checks for updates, it fetches
a manifest JSON from:

```
https://raw.githubusercontent.com/hneogy/Ugh-Storage--Setup-Files/stable/server/manifest.json
```

The manifest carries an **Ed25519 signature** over its contents. The
public key is baked into your Pi's `update_manager.py` at install time.
When the Pi fetches an update, it verifies the signature against that
public key before touching anything:

```
  Pi boots               ┌──────────────────────┐           ┌────────────────────┐
  or polls  ────────────▶│  GitHub raw URL      │──HTTPS───▶│  manifest.json     │
                         │  (signed manifest)   │           │  { version, sig }  │
                         └──────────────────────┘           └─────────┬──────────┘
                                                                      │
                                                                      ▼
                                                        ┌────────────────────────┐
                                                        │  update_manager.py     │
                                                        │    verify Ed25519 sig  │
                                                        │    against baked-in    │
                                                        │    public key          │
                                                        └───────────┬────────────┘
                                                                    │
                                                      ✅ signature OK │
                                                                    ▼
                                                        ┌────────────────────────┐
                                                        │  update.sh             │
                                                        │    git fetch + checkout│
                                                        │    pip install         │
                                                        │    restart service     │
                                                        │    health-check verify │
                                                        │    rollback on failure │
                                                        └────────────────────────┘
```

If anyone hijacks our GitHub account and pushes a bad manifest, or
MITMs the manifest URL, their update is **rejected before download**
because they can't produce a matching signature without the private
key (which lives only on the developer's machine, never in any repo or
CI system).

---

## Is this for me?

**Yes, if you:**
- Have an iPhone running iOS 17 or later
- Are comfortable with 30 minutes of SSH commands once, then never again
- Want a one-bill (electricity) solution instead of monthly subscriptions
- Care about keeping photos / files out of big-cloud providers

**Not yet, if you:**
- Don't own an iPhone (no Android client yet)
- Need true 24/7 enterprise uptime (a home Pi is home-reliable, not
  data-center-reliable)
- Plan to have 10+ simultaneous Jellyfin transcodes (the Pi isn't
  powerful enough; use Infuse / direct-play clients instead)

**Two paths to set up:**

1. **You buy a pre-flashed Ugh! Storage device** from us. It arrives
   with an [activation code](#activation-codes-why-you-need-one) on a
   sticker. Skip to step 8 below — everything else is done.

2. **You build your own from scratch** using this repo. You'll need an
   activation code for the registration step (see
   [activation codes](#activation-codes-why-you-need-one)) or you fork
   the project and point it at your own Supabase backend. Follow the
   full setup below.

---

## What you'll need

### Hardware (~$130 one-time)

| Item | Approx. cost | Notes |
|---|---|---|
| [Raspberry Pi 5 (8 GB or 16 GB)](https://www.raspberrypi.com/products/raspberry-pi-5/) | $80–$120 | 8 GB is the minimum for photos + video ML. 16 GB is comfortable for running everything |
| NVMe SSD (256 GB – 2 TB) | $25–$100 | M.2 2230 or 2242 |
| [NVMe HAT](https://pimoroni.com/nvmebase) | $15 | Pimoroni, Geekworm, etc. |
| 27W USB-C power supply | $12 | Use the [official one](https://www.raspberrypi.com/products/27w-usb-c-power-supply/). Cheap chargers cause mystery corruption. |
| 32 GB+ microSD card | $8 | Any brand for booting |

### Software

- An iPhone on iOS 17+
- A Mac / Windows / Linux machine to flash the SD card

---

## Setup guide

**Total time: ~30 minutes.** One-time.

### 1. Assemble the hardware (5 min)

1. Snap the NVMe SSD into the NVMe HAT.
2. Connect the HAT to the Pi via the ribbon cable.
3. Insert the microSD card.
4. Don't power on yet.

If you have a case, assemble it now. The NVMe can get warm under load
— a ventilated case helps.

### 2. Flash the operating system (10 min)

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Pick: Device → **Raspberry Pi 5**. OS → **Raspberry Pi OS Lite (64-bit)**. Storage → your SD card.
3. Before writing, click the gear ⚙️ and set:
   - Hostname: `ughstorage`
   - Enable SSH (password auth)
   - Username: `pi`. Set a strong password.
   - Wi-Fi: your home network + password (this is just for initial SSH; the app replaces it later)
4. Write. Takes ~5 minutes. Insert SD into the Pi, plug in power.
5. Wait ~2 minutes for first boot.

### 3. SSH in (2 min)

From your Mac / PC:

```bash
ssh pi@ughstorage.local
```

If that fails, try `ssh pi@raspberrypi.local`, or look up the Pi's IP
in your router's admin page and use that. Enter the password you set.

### 4. Format the NVMe SSD (5 min)

```bash
# Confirm the Pi sees the drive
lsblk     # expect a line for nvme0n1

# Partition it (one partition, full drive)
sudo fdisk /dev/nvme0n1
# Interactive prompts: n → enter → enter → enter → enter → w

# Format ext4
sudo mkfs.ext4 /dev/nvme0n1p1

# Mount to /mnt/nvme
sudo mkdir -p /mnt/nvme
sudo mount /dev/nvme0n1p1 /mnt/nvme
sudo chown pi:pi /mnt/nvme

# Mount on every boot
echo '/dev/nvme0n1p1 /mnt/nvme ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab

# Sanity check — should show your drive's full capacity
df -h /mnt/nvme
```

### 5. Install the Ugh! Storage server (5 min)

```bash
cd /home/pi
git clone https://github.com/hneogy/Ugh-Storage--Setup-Files.git
cd Ugh-Storage--Setup-Files/server
chmod +x setup.sh ble_setup_service.sh
./setup.sh
```

`setup.sh` installs:
- Python, ffmpeg, git, Docker
- Cloudflared (used later to create your device's tunnel)
- The FastAPI server, a virtualenv, and the `ughstorage` systemd unit
- Storage directories at `/mnt/nvme/storage`, `/mnt/nvme/thumbnails`,
  `/mnt/nvme/ughstorage/{media,module-data}/…`
- `/var/lib/ughstorage/` for OTA state tracking
- A scoped sudoers rule so the server can restart itself and module
  units without a password (nothing else bypasses sudo)

> **Log out and back in after this runs.** It adds your user to the
> `docker` group and that membership doesn't take effect until your
> next login session. Or run `newgrp docker` to activate it in the
> current shell.

### 6. Install the Bluetooth setup service (2 min)

```bash
sudo bash ble_setup_service.sh
```

Your Pi starts advertising as `UghStorage-Setup` over Bluetooth. This
is what the iOS app finds.

### 7. Start the server

```bash
sudo systemctl start ughstorage
sudo systemctl status ughstorage     # should show "active (running)"

# Quick sanity check
curl http://localhost:8000/health    # returns a JSON health envelope
```

### 8. Configure OTA updates (recommended)

Tell the server where to check for updates. Edit `server/.env`:

```bash
nano /home/pi/Ugh-Storage--Setup-Files/server/.env
```

Add this line:

```
UGHSTORAGE_UPDATE_MANIFEST_URL=https://raw.githubusercontent.com/hneogy/Ugh-Storage--Setup-Files/stable/server/manifest.json
```

Optional but recommended — require signed manifests only:

```
UGHSTORAGE_UPDATE_REQUIRE_SIGNATURE=1
```

Restart to apply:

```bash
sudo systemctl restart ughstorage
```

Your Pi will now offer you an "Update available" banner in the iOS app
whenever we ship a new signed release.

### 9. Pair from the iOS app

1. Download the Ugh! Storage app from the App Store. *(Link coming soon — join the waitlist at ughstorage.com)*
2. Create an account in the app.
3. Tap **Add Device**. The app walks you through:
   1. **Find your Pi** — Bluetooth scan. Tap the Pi when it appears.
   2. **Pick Wi-Fi** — list of networks the Pi can see.
   3. **Wi-Fi password** — the app sends it to the Pi over BLE.
   4. **Name your device**.
   5. **Activation code** — type or scan the QR on your device's
      sticker. If you built your Pi yourself, see
      [activation codes](#activation-codes-why-you-need-one).
   6. **Register** — the Pi calls our cloud, which validates the code,
      creates a unique `<subdomain>.ughstorage.com`, and provisions a
      Cloudflare Tunnel.
   7. **Done.**

Your Pi is now live at a stable URL, reachable only to your account.

---

## Activation codes (why you need one)

Every registered Ugh! Storage device is tied to a **provisioning
token** — a unique sticker code in the format
`UGH-XXXX-XXXX-XXXX-X`. You type (or QR-scan) it into the iOS app
during setup.

**Why it exists:** the server repo is public so anyone can audit what
their Pi actually runs. But registering a device against our cloud
costs us real money (Supabase rows + Cloudflare tunnel quota), so we
gate registration behind a token. One sticker per Pi. No token, no
registration against *our* backend.

**How to get one:**

- **If you bought a device from us:** it's on a sticker on the bottom
  of the device. First person to redeem claims it; the same person can
  factory-reset and re-redeem freely. A different user can only claim
  it if the original owner unlinks first.
- **If you built your own Pi for personal use:** email
  honorius@neogy.dev with your GitHub username — we hand out tokens to
  self-hosters on request.
- **If you're developing a fork:** the cleaner path is to stand up
  your own Supabase project, clone the `edge-functions/` directory,
  deploy it, and remove the provisioning-token check entirely. The
  token gate exists for *our* cloud economics, not as a technical
  requirement. Full details in [`edge-functions/`](./edge-functions/).

The code format has a checksum so typos fail instantly before any
network request.

---

## The modules explained

All modules are **off by default** except Storage. Enable them from
the iOS app's Settings once your device is paired. Each module's
management view shows install/uninstall, credentials, and a handoff to
the best third-party iOS app for daily use.

### Storage (always on — the thing the iOS app talks to)

- Runs natively on the Pi as a Python process (`ughstorage.service`)
- Port 8000, served through the Cloudflare Tunnel
- Owns all the file management: upload / download / search / thumbnails
  / trash / share links / HLS video transcodes

### Music — Navidrome

- Pinned version: Navidrome 0.54.1 (Docker)
- Service: `ugh-module-music.service`, port 4533 (LAN only)
- Data:
  - `/mnt/nvme/ughstorage/media/music/` — your library (read-only to Navidrome)
  - `/mnt/nvme/ughstorage/module-data/music/` — its config and database
- Subsonic-compatible. Use Substreamer, Amperfy, play:Sub, or any
  other Subsonic client. Credentials are shown in the iOS management
  view.
- **Currently LAN-only.** Sprint to route it through the Cloudflare
  Tunnel is planned.

### Photos — Immich

- Pinned version: Immich v1.140.0 (Docker Compose — server, ML,
  Postgres, Redis)
- Service: `ugh-module-photos.service`, port 2283 (LAN only)
- Data:
  - `/mnt/nvme/ughstorage/media/photos/upload/` — your photos
  - `/mnt/nvme/ughstorage/module-data/immich/` — Postgres + ML models
- Face recognition and visual search run on the Pi's GPU when enabled.
  The iOS management view has a toggle for ML (~2–4 GB RAM when on).
- Use the official Immich iOS app. Copy the server URL + admin email
  + password from the iOS management view.
- Hardware floor: 6 GB RAM for ML features. Pi 5 4 GB runs photo
  backup without ML; Pi 5 8 GB / 16 GB run everything.

### Video — Jellyfin

- Pinned version: Jellyfin 10.9.11 (Docker)
- Service: `ugh-module-video.service`, port 8096 (LAN only)
- Data:
  - `/mnt/nvme/ughstorage/media/video/` — your library (read-only to Jellyfin)
  - `/mnt/nvme/ughstorage/module-data/jellyfin/` — config + watch history
- **Transcoding on a Pi is limited.** The management view says this
  loudly. Best playback on Pi comes from clients that direct-play —
  Infuse (paid) is the recommendation; the free Jellyfin iOS app works
  too for compatible codecs.

**Enable / disable any module** from the iOS Settings screen. Install
fetches the Docker image, seeds credentials, and starts the unit.
Uninstall stops the unit and removes the container but **preserves
your library files and the module's data** — reinstalling brings you
right back.

---

## Updates: how new versions reach your Pi

The manifest at
[`server/manifest.json`](./server/manifest.json) on the `stable` branch
of this repo is the source of truth.

When we cut a new release:

1. Tests pass.
2. We sign a new `manifest.json` with an offline Ed25519 key (the
   public half is baked into your Pi at install time).
3. We push the signed manifest + updated code to the `stable` branch.

Your Pi notices on the next check cycle. In the iOS Settings → About
row, you see **"Update available: X.Y.Z"** with release notes.

- **Tap Update** → Pi records the current git SHA, pulls the new code,
  reinstalls Python deps, restarts the service.
- **Health check fails** (any reason — syntax error, missing dep, new
  service fails to start) → Pi rolls back to the previous SHA
  automatically, tells you "Update rolled back" with the reason.

You never lose data during an update. Rollback is automatic. No SSH
required for any of this.

Module upgrades work the same way: when a new release bumps the
pinned Navidrome / Immich / Jellyfin version, you see an "Update
available" chip in that module's settings card. Tap to reinstall; data
is preserved.

---

## Managing your device

### From the iOS app (the normal path)

Settings gives you:

- Device dashboard with aggregated health and storage breakdown
- Per-module install/uninstall, credentials, version
- Wi-Fi switching (over Bluetooth, no SSH)
- Factory reset / unlink flows
- Account security: password, 2FA, linked social accounts
- Smart Search toggle (on-device OCR + face clustering privacy control)
- Data export (zip of all files + metadata manifest)
- OTA update controls

### From SSH (advanced)

```bash
# Main server
sudo systemctl status ughstorage
journalctl -u ughstorage -f

# Module services
sudo systemctl status ugh-module-music ugh-module-photos ugh-module-video

# Cloudflare tunnel
sudo systemctl status cloudflared

# Disk
df -h /mnt/nvme

# CPU temperature
vcgencmd measure_temp

# Update state (useful during OTA)
cat /var/lib/ughstorage/update-state.json
```

### Version pinning for each module

The exact version each module installs at is in the install scripts:

| Module | Where | Current |
|---|---|---|
| Navidrome | [`server/install_music.sh`](./server/install_music.sh) — `NAVIDROME_VERSION` | 0.54.1 |
| Immich | [`server/install_photos.sh`](./server/install_photos.sh) — `IMMICH_VERSION` | v1.140.0 |
| Jellyfin | [`server/install_video.sh`](./server/install_video.sh) — `JELLYFIN_VERSION` | 10.9.11 |

We bump these only after smoke-testing on our own Pi, then release the
change through the signed OTA mechanism above.

---

## Troubleshooting

<details>
<summary><strong>"Can't find device" during Bluetooth setup</strong></summary>

- Stay within ~10 feet of the Pi
- Bluetooth enabled on the iPhone (Settings → Bluetooth)
- Check on the Pi: `sudo systemctl status ughstorage-ble`
- Restart: `sudo systemctl restart ughstorage-ble`
</details>

<details>
<summary><strong>"Server unreachable" in the app</strong></summary>

- Tunnel status: `sudo systemctl status cloudflared`
- Server status: `sudo systemctl status ughstorage`
- Local test: `curl http://localhost:8000/health`
- Logs: `journalctl -u ughstorage -f`
</details>

<details>
<summary><strong>"This activation code is already linked to another account"</strong></summary>

A previous owner claimed the code. Ask them to unlink the device from
their account (Settings → Danger Zone → Unlink Device). Then you can
re-register with the same code.
</details>

<details>
<summary><strong>"We don't recognize that activation code"</strong></summary>

Double-check the code for typos (the checksum character at the end is
common to mistype). If you bought a device from us and it's definitely
the code on the sticker, email honorius@neogy.dev with your order
number.
</details>

<details>
<summary><strong>Uploads fail with "Device is nearly full"</strong></summary>

The Pi refuses new uploads when less than 1 GB free remains — this
protects system stability. Free up space from the iOS app's Duplicates
/ Free Up Space views or delete files from Storage.
</details>

<details>
<summary><strong>Module install hangs at "Pulling image"</strong></summary>

First install pulls ~1.5 GB for Immich. On slow home internet this
can take 10–15 minutes. Check `docker ps` on the Pi to see what's
happening, or tail the install log:

```bash
tail -f /var/lib/ughstorage/module-*-install.log
```
</details>

<details>
<summary><strong>Jellyfin can't play a specific file</strong></summary>

- Your Pi probably can't transcode it. Use Infuse or Jellyfin on an
  Apple TV / iPhone — they direct-play more codecs than the Pi can
  transcode.
- Check Jellyfin logs: `docker logs ugh-jellyfin`
</details>

<details>
<summary><strong>OTA update rolled back</strong></summary>

The new version failed its post-restart health check within 60
seconds. The `update.log` at `/var/lib/ughstorage/update.log` has
details. Contact support with a snippet and we'll investigate; the
rollback itself is safe — you're running the version you had before.
</details>

<details>
<summary><strong>Pi lost power / rebooted</strong></summary>

Everything auto-starts. Give it ~1 minute for Wi-Fi + tunnel to come
back. If anything's wrong, the iOS app's device dashboard will show a
red health dot with details.
</details>

See also [`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md) for
in-depth diagnostics.

---

## Frequently asked questions

**Q: Do you see my files?**
No. They're stored on your NVMe drive and served from your Pi.
Cloudflare sees encrypted traffic passing through. Our backend stores
your device record (email, tunnel URL, storage totals for your
dashboard) — we do not store filenames, content, or thumbnails.

**Q: What if Cloudflare changes their pricing?**
The Cloudflare Tunnel feature is currently free and generous. If that
ever changes, we'd migrate to [Tailscale](https://tailscale.com/) or
similar. The iOS app's URL resolution is already structured to support
multiple reach paths; swapping the underlying transport would be a
behind-the-scenes change.

**Q: Can I use my own Supabase backend?**
Yes — fork this repo, deploy the edge functions in `edge-functions/`
to your own Supabase project, change `UGHSTORAGE_SUPABASE_URL` and
`UGHSTORAGE_SUPABASE_ANON_KEY` in your Pi's `.env` and the iOS app
constants to point at your project. You'll also want to regenerate
your own Ed25519 signing key so your OTA manifests verify against your
own public key.

**Q: Does it work when my home internet is down?**
Modules accessed over the tunnel (the Ugh! Storage app from outside
your home) obviously don't work without internet. If you're on the
same Wi-Fi, direct-LAN access to Music / Photos / Video still works
because those bind locally.

**Q: What happens if my Pi breaks?**
Your files are still on the NVMe drive. Pull the drive, plug it into
any Linux machine (or a new Pi), follow the setup steps, and your
files come right back. The Cloudflare tunnel gets re-provisioned
during re-registration. You'll keep the same account but a new
`subdomain.ughstorage.com`.

**Q: Can multiple people share one Pi?**
One account owns the Pi today. Family sharing / multi-user is on the
roadmap but not live yet. Each additional person would need their own
iCloud family Pi for now.

**Q: Is there an Android app?**
Not yet.

**Q: Can I self-host this commercially?**
The code is MIT-licensed (see [LICENSE](./LICENSE)). The bundled
media modules have their own licenses — see the next section.

---

## Licenses of components we bundle

| Component | License | What that means |
|---|---|---|
| Ugh! Storage server code (this repo) | MIT | Use / modify / ship freely |
| Navidrome (music module) | [GPL-3.0](https://github.com/navidrome/navidrome/blob/master/LICENSE) | We ship the unmodified Docker image. If you fork + modify Navidrome, GPL requires you to publish the modified source. |
| Immich (photos module) | [AGPL-3.0](https://github.com/immich-app/immich/blob/main/LICENSE) | We ship the unmodified Docker image. AGPL requires publishing modifications if you expose your modified version over the network to other users. |
| Jellyfin (video module) | [GPL-2.0](https://github.com/jellyfin/jellyfin/blob/master/LICENSE) | We ship the unmodified Docker image. |
| Cloudflare Tunnel (cloudflared) | [Apache-2.0](https://github.com/cloudflare/cloudflared/blob/master/LICENSE) | Free to use. |
| Supabase Edge Runtime / Postgres | Permissive / Postgres | We run our own Supabase instance; you can run your own. |

None of this is legal advice. If you're shipping commercially, get a
lawyer to review. The key practical rule: **don't modify the bundled
Docker images**. Ship upstream versions as-is, and you stay out of
license-compliance headaches.

---

## Contributing

Bug reports and feature requests: open an issue on this repo. Please
include:
- Pi model + RAM
- Server version (`curl http://localhost:8000/health`)
- Relevant logs (`journalctl -u ughstorage --no-pager | tail -50`)

Code contributions: PRs welcome. Please open an issue first for
anything non-trivial so we can agree on the approach.

For security reports: email honorius@neogy.dev directly, don't open a
public issue.

---

## License

[MIT](./LICENSE) for the code in this repo. Third-party components
retain their own licenses — see the [table above](#licenses-of-components-we-bundle).
