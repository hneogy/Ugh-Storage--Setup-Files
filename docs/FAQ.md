# UghStorage — Frequently Asked Questions

Everything you might want to know before buying, building, or using UghStorage.

---

## Before You Buy

### What exactly is UghStorage?

UghStorage is a personal cloud storage system. You buy a Raspberry Pi 5, attach an NVMe SSD, run our setup scripts, and download our iPhone app. The result is your own private cloud — like iCloud or Google Drive, except your files live on hardware you own, in your home, and nobody else can access them.

### How is this different from iCloud / Google Drive / Dropbox?

| | UghStorage | iCloud / Google Drive |
|---|---|---|
| **Where are your files?** | On an SSD in your home | On corporate servers |
| **Who can access them?** | Only you | The company, governments with warrants, hackers if breached |
| **Monthly cost** | ~$2 (electricity) | $3-10/month |
| **Storage limit** | Whatever SSD you buy (up to 4TB) | Whatever plan you pay for |
| **Can they raise prices?** | No | Yes, and they do |
| **Can they scan your files?** | No | Yes — for AI training, ads, "safety" |
| **Works if company shuts down?** | Yes | No |

### How much does it cost?

**One-time hardware:** ~$100-150 depending on SSD size
- Raspberry Pi 5: $60-80
- NVMe SSD: $25-100 (256GB to 2TB)
- NVMe HAT: ~$15
- Power supply: ~$12
- MicroSD card: ~$8

**Ongoing:** ~$25/year
- Domain name: ~$10/year
- Electricity: ~$15/year
- Cloudflare: Free

After the first year, you're spending about **$2/month** — compared to $10/month for 2TB of iCloud.

### Do I need to be technical to set this up?

You need to be comfortable with:
- Plugging hardware together (like building with LEGO)
- Typing commands into a terminal (we give you every command to copy-paste)
- Following step-by-step instructions

You do **not** need to know how to code, understand networking, or have any server experience. The setup takes about 30 minutes and the README walks you through every step.

### What iPhone do I need?

Any iPhone running **iOS 17 or later** with Bluetooth. That includes iPhone XS and newer.

### Does it work with Android?

Not yet. The app is iOS-only for now. Android support may come in the future.

### Does it work with iPad / Mac?

The app is designed for iPhone. iPad support is possible through the iPhone app (it will run in compatibility mode). There's no dedicated Mac app, but you can access your files through the API if you're technical.

### How much storage can I get?

Whatever NVMe SSD you buy. Common options:
- **256GB** (~$25) — good for documents and some photos
- **512GB** (~$35) — good for most people
- **1TB** (~$50-60) — great for photos, videos, and documents
- **2TB** (~$100) — heavy media storage
- **4TB** (~$200) — maximum capacity with M.2 2242 drives

You can swap the SSD at any time if you need more space.

### Can I upgrade the storage later?

Yes. Power down the Pi, swap the NVMe SSD for a bigger one, format it, and restore from backup. Or start fresh — the app will just see an empty device and you can re-upload.

---

## Setup & Installation

### How long does setup take?

About **30 minutes** from unboxing to uploading your first file:
- Assembling hardware: 5 min
- Flashing the OS: 10 min
- SSH + mounting the SSD: 7 min
- Running setup scripts: 5 min
- App setup (Bluetooth + WiFi): 3 min

### What does the setup script actually install?

The `setup.sh` script installs:
- **Python 3** and a virtual environment (isolated from system Python)
- **ffmpeg** (for generating video thumbnails)
- **cloudflared** (Cloudflare's tunnel client)
- Python packages: FastAPI, uvicorn, aiosqlite, Pillow, PyJWT, etc.
- Storage directories on your NVMe SSD
- A systemd service so the server starts automatically on boot

The `ble_setup_service.sh` script installs:
- **BlueZ** (Linux Bluetooth stack)
- **NetworkManager** (for WiFi management)
- **dbus-next** (Python library for Bluetooth)
- A systemd service for the Bluetooth provisioning server

### Do I need to buy a domain name?

No. Your device gets a unique URL under `ughstorage.com` automatically during registration. You don't need to buy, configure, or manage any domain.

### Do I need a Cloudflare account?

No. The Cloudflare Tunnel is provisioned automatically through `ughstorage.com` when you register your device. You don't interact with Cloudflare at all.

### Can I use a Raspberry Pi 4?

Not recommended. The Pi 5 has:
- A PCIe bus for NVMe storage (the Pi 4 only has USB 3.0, which is slower)
- Better CPU for thumbnail generation
- Built-in Bluetooth 5.0 for the BLE setup flow

The software might technically work on a Pi 4 with a USB SSD, but it's not tested or supported.

### Can I use a USB hard drive instead of NVMe?

Technically yes — you'd mount it at `/mnt/nvme` (or change the paths in `.env`) — but NVMe is strongly recommended. USB storage is significantly slower for random read/write operations, which affects thumbnail generation and file browsing.

### What if I already have Raspberry Pi OS installed?

That's fine. You can skip Step 2 (flashing). Just make sure you're running **Raspberry Pi OS Lite (64-bit)** — the desktop version works too but wastes resources. SSH in, mount your NVMe SSD, and continue from Step 5.

### The Pi can't find my NVMe SSD

- Make sure the ribbon cable is fully seated on both ends
- Try a different NVMe drive — not all M.2 drives are compatible with all HATs
- Run `lsblk` and `lspci` to check if the Pi detects the PCIe device
- Some HATs require a firmware update on the Pi. Run `sudo rpi-update` and reboot

---

## Network & Connectivity

### How does remote access work without opening ports?

UghStorage uses **Cloudflare Tunnel** — an outbound-only connection from your Pi to Cloudflare's network. Here's the key insight:

Traditional port forwarding: You open a port on your router → anyone on the internet can try to connect → security risk.

Cloudflare Tunnel: Your Pi connects *out* to Cloudflare → Cloudflare routes your app's traffic through that connection → no ports opened, no exposure.

It's like making a phone call vs. leaving your front door open. The Pi calls out, and only verified traffic comes back through that call.

### What happens if my internet goes down?

The Pi keeps running locally, but you can't access it remotely until your internet comes back. When it does, the Cloudflare Tunnel reconnects automatically — usually within seconds. No manual intervention needed.

If you're on the same WiFi as the Pi, you can still access it locally at `http://ughstorage.local:8000` (or whatever hostname you set).

### What happens if the power goes out?

When power returns, the Pi boots up automatically and all three services (server, BLE, tunnel) start on their own. Your files are safe on the NVMe SSD. Give it about 1-2 minutes after power returns before trying to connect.

### Can I access my files from cellular (not WiFi)?

Yes. That's the whole point of the Cloudflare Tunnel. Whether you're on WiFi, cellular, or a coffee shop network — the app connects to `ughstorage.com` which routes to your Pi. Works anywhere you have internet.

### How fast are uploads and downloads?

Speed depends on two things:
1. **Your home internet upload speed** — this is usually the bottleneck. If your ISP gives you 10 Mbps upload, that's your max remote upload speed.
2. **Cloudflare's tunnel overhead** — minimal, usually adds <10ms latency.

On the same local network as the Pi, speeds are limited by WiFi (typically 50-100 MB/s on Wi-Fi 6).

### Can I use this while traveling internationally?

Yes. The Cloudflare Tunnel works from anywhere in the world. Your Pi stays at home, and you access it through `ughstorage.com` regardless of where you are.

### What if I move to a new house?

Open the UghStorage app near your Pi (within Bluetooth range), go to Settings, and reconfigure WiFi. The app connects to the Pi over Bluetooth, sends the new WiFi credentials, and the tunnel reconnects automatically. No SSH, no terminal needed.

---

## Storage & Files

### What file types can I upload?

Anything. Photos, videos, PDFs, documents, archives, music — there's no restriction on file types. The app generates thumbnails for images and videos automatically.

### What's the maximum file size?

**5GB per file** by default. This can be changed in the server configuration (`UGHSTORAGE_MAX_UPLOAD_SIZE` in `.env`) if you need larger uploads.

### Can I share files with other people?

Yes. In the app, long-press any file and tap "Share." This generates a unique link that anyone can use to download the file — no account needed. You can set share links to expire after a certain time.

### Can I organize files into folders?

Yes. The app supports creating folders, moving files between folders, and navigating your file tree. You can also search across all folders.

### What happens when I delete a file?

Deleted files go to **Trash** first. You can restore them from Trash or permanently delete them. "Empty Trash" permanently removes all trashed files.

### Can I mark files as favorites?

Yes. Long-press any file and tap the star. Favorites appear in a dedicated section in the app for quick access.

### Do photos back up automatically?

Yes — the app has a photo backup feature. Enable it in Settings and it will automatically upload new photos from your Camera Roll to your Pi.

### Is there a storage limit?

Only the physical capacity of your NVMe SSD. There are no artificial limits, no tier restrictions, no "you need to upgrade your plan" messages.

---

## Security & Privacy

### Who can see my files?

Only you. Your files are stored on your NVMe SSD in your home. The server requires JWT authentication with a per-device cryptographic secret for every API request. Even Cloudflare can't read your files — they just pass encrypted traffic through.

### Is the connection encrypted?

Yes. All traffic between your iPhone and your Pi is encrypted with TLS (HTTPS) through the Cloudflare Tunnel. This is the same encryption used by banks and government websites.

### What if someone steals my Pi?

Your files would be on the SSD, unencrypted at rest (the same as a regular external drive). If physical security is a concern, you can enable full-disk encryption on the NVMe SSD using LUKS — but this requires manual setup and you'll need to enter the decryption password on every boot.

The app itself uses biometric authentication (Face ID / Touch ID) so even if someone has your phone, they can't access the storage without your face or fingerprint.

### Can Cloudflare see my files?

Cloudflare terminates TLS at their edge, so technically the traffic is decrypted momentarily at their servers before being re-encrypted through the tunnel. However, Cloudflare processes billions of requests daily and does not inspect or store file contents. If this concerns you, the app supports client-side encryption — files are encrypted on your iPhone before upload, so the Pi (and Cloudflare) only ever see encrypted blobs.

### What data does UghStorage (the company) have access to?

The UghStorage backend (Supabase) stores:
- Your account (email, hashed password)
- Device metadata (device ID, last seen timestamp, storage stats)
- Tunnel provisioning tokens

It does **not** store your files, file names, file contents, or any file metadata. Your files exist only on your Pi.

### What happens if UghStorage (the company) shuts down?

Your Pi keeps working for local access. The Cloudflare Tunnel would eventually stop if the tunnel tokens expire, but your files remain on your hardware. You'd need to set up your own Cloudflare Tunnel for remote access (free, ~10 minutes of work).

---

## Hardware & Maintenance

### How much power does the Pi 5 use?

About **5-12 watts** depending on load. That's roughly **$10-15/year** in electricity in the US. It's comparable to leaving an LED bulb on.

### Does it run 24/7?

Yes. The Pi is designed to run continuously. It uses solid-state storage (no spinning hard drives), has no fans (unless your case has one), and the NVMe SSD has no moving parts. It's designed to be plugged in and forgotten.

### How hot does it get?

The Pi 5 can reach 60-80C under heavy load (like generating many thumbnails at once). Normal operation is 40-55C. A case with passive heatsinks or a small fan keeps it cool. The app shows CPU temperature in Settings → Device Info.

If the Pi gets too hot, it will throttle its CPU speed rather than shut down — so performance drops but nothing breaks.

### How long will the hardware last?

Raspberry Pis are rated for years of continuous operation. NVMe SSDs are rated for hundreds of terabytes of writes (a typical user won't hit this in 10+ years). The most likely failure point is the MicroSD card, but since all your data is on the NVMe SSD, you'd just re-flash the MicroSD and re-run setup.

### Can I run other things on the Pi?

Yes, but be careful. UghStorage uses minimal resources (typically <10% CPU, <200MB RAM), so you have plenty of headroom. Just don't run anything that conflicts with port 8000 or the Bluetooth adapter.

### Where should I put the Pi?

Anywhere with:
- Power outlet
- WiFi signal (or Ethernet — the Pi 5 has a gigabit Ethernet port)
- Some ventilation (don't put it in a sealed box)

A bookshelf, desk, or closet shelf works great. It doesn't need to be near your router — WiFi is fine.

---

## App Features

### Can multiple people use the same device?

Yes. UghStorage supports multiple user accounts per device. Each user signs in with their own account and has access to the shared storage. Files aren't isolated per-user by default — all users see all files.

### Can I have multiple Pi devices on one account?

Yes. Add as many devices as you want. Each one shows up in the app and you can switch between them.

### Does the app work offline?

The app needs an internet connection to access your Pi remotely. If you're on the same local network as your Pi, you can access it over the local network even without internet.

### Can I download files for offline access?

Yes. Tap any file to view it and use the download/share button to save it locally to your iPhone.

### What happens to my photos if I switch phones?

Sign into the UghStorage app on your new phone with the same account. All your files are on the Pi — nothing is stored on the phone itself.

---

## Troubleshooting (Quick Answers)

### The app can't find my Pi during setup

Make sure you're within ~10 feet with Bluetooth enabled on your iPhone. On the Pi, run `sudo systemctl status ughstorage-ble` to verify the Bluetooth service is running. Try `sudo systemctl restart ughstorage-ble` to restart it.

### I forgot my Pi's SSH password

Re-flash the MicroSD card with Raspberry Pi Imager and set a new password. Your files on the NVMe SSD are untouched — just re-mount the SSD and re-run the setup scripts.

### How do I factory reset?

**From the app:** Settings → Danger Zone → Factory Reset. This deregisters the device, stops the tunnel, and clears credentials. The Pi becomes discoverable over Bluetooth again for re-setup.

**From SSH:** You can manually clear credentials with:
```bash
sudo systemctl stop cloudflared
sudo systemctl stop ughstorage
# Edit /home/pi/ughstorage/server/.env and clear DEVICE_ID and DEVICE_SHARED_SECRET
sudo systemctl restart ughstorage-ble
sudo systemctl start ughstorage
```

### How do I update the server software?

```bash
ssh pi@ughstorage.local
cd /home/pi/ughstorage
git pull
cd server
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ughstorage
```

### Can I see server logs?

```bash
# Main server
journalctl -u ughstorage -f

# Bluetooth service
journalctl -u ughstorage-ble -f

# Cloudflare tunnel
journalctl -u cloudflared -f
```

For more detailed troubleshooting, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
