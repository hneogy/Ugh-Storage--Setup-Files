# UghStorage

**Your own personal cloud. No subscriptions. No data mining. Just your files, on your hardware, accessible from anywhere.**

UghStorage turns a Raspberry Pi 5 + NVMe SSD into your own private cloud storage device. Upload photos, videos, documents — anything — and access them from your iPhone wherever you are. Your files never touch anyone else's servers.

---

## How It Works

1. You build a simple Pi setup at home (30 min, one-time)
2. You download the UghStorage app on your iPhone
3. The app finds your Pi over Bluetooth, connects it to WiFi, and registers it
4. That's it — your personal cloud is live. Upload files from anywhere.

---

## Architecture

### Everyday Usage

When the device is set up, this is how your files flow:

```
                        ┌─────────────────────────────────────────────┐
                        │              ughstorage.com                 │
                        │          (Cloudflare Network)               │
    ┌──────────┐        │                                             │        ┌──────────────────┐
    │  iPhone   │       │   ┌───────────────────────────────────┐     │        │  Your Pi (Home)  │
    │  App      │──────▶│   │  Encrypted Tunnel (TLS/HTTPS)     │─────│──────▶ │                  │
    │          │◀──────│   │  ◀──────────────────────────────── │◀────│─────── │  FastAPI Server  │
    └──────────┘        │   └───────────────────────────────────┘     │        │  SQLite DB       │
    Anywhere in         │                                             │        │  NVMe SSD Files  │
    the world           │  No ports opened on your home network.      │        └──────────────────┘
                        │  Pi connects outbound only.                 │         Always on, at home
                        └─────────────────────────────────────────────┘
```

Your iPhone talks to `ughstorage.com`, which routes through a Cloudflare Tunnel straight to your Pi. Your files never leave your hardware — Cloudflare just passes the encrypted traffic through. No ports are opened on your home router.

### First-Time Setup (via Bluetooth)

The Pi starts with no WiFi and no internet. The app sets everything up over Bluetooth:

```
    ┌──────────────┐    Bluetooth     ┌──────────────┐      WiFi       ┌──────────┐
    │              │   ──────────▶    │              │   ──────────▶   │          │
    │   iPhone     │   1. Find Pi     │   Pi         │   3. Connect    │  Router  │
    │   App        │   2. Send WiFi   │   BLE Server │      to WiFi   │          │
    │              │      password    │              │                 │          │
    └──────────────┘                  └──────┬───────┘                 └──────────┘
                                             │
                                             │ 4. Pi registers with ughstorage.com
                                             │    via Supabase edge function
                                             ▼
                                    ┌──────────────────┐
                                    │  Cloudflare       │
                                    │  Tunnel created   │
                                    │  automatically    │
                                    └──────────────────┘
                                             │
                                             ▼
                                    ┌──────────────────┐
                                    │  Device live at   │
                                    │  *.ughstorage.com │
                                    │                   │
                                    │  App switches to  │
                                    │  cloud access     │
                                    └──────────────────┘
```

**Step by step:**
1. The app discovers the Pi over Bluetooth (BLE) — no network needed
2. You pick your WiFi network and the app sends the credentials to the Pi over BLE
3. The Pi connects to your WiFi router
4. The Pi registers itself with `ughstorage.com` and a Cloudflare Tunnel is auto-provisioned
5. The app switches from Bluetooth to cloud access — setup is done

### Moving to a New House?

Just open the app near your Pi and reconfigure WiFi over Bluetooth. The tunnel reconnects automatically:

```
Phone app  ──▶  Bluetooth  ──▶  Pi  ──▶  New WiFi credentials  ──▶  Tunnel reconnects
```

No SSH, no terminal, no technical knowledge needed.

### What Runs on the Pi

```
┌─────────────────────────────────────────────────────────────┐
│                     Raspberry Pi 5                          │
│                                                             │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  ughstorage     │  │  ughstorage  │  │  cloudflared  │  │
│  │  (FastAPI)      │  │  -ble        │  │  (tunnel)     │  │
│  │                 │  │  (Bluetooth) │  │               │  │
│  │  File upload    │  │              │  │  Routes all   │  │
│  │  Download       │  │  WiFi setup  │  │  traffic from │  │
│  │  Search         │  │  Device      │  │  ughstorage   │  │
│  │  Thumbnails     │  │  registration│  │  .com to      │  │
│  │  Trash          │  │              │  │  localhost     │  │
│  │  Sharing        │  │              │  │  :8000        │  │
│  │  Favorites      │  │              │  │               │  │
│  │  Device info    │  │              │  │               │  │
│  └────────┬────────┘  └──────────────┘  └───────────────┘  │
│           │                                                 │
│  ┌────────┴────────┐  ┌──────────────────────────────────┐  │
│  │  SQLite DB      │  │  /mnt/nvme (NVMe SSD)            │  │
│  │  (metadata,     │  │                                  │  │
│  │   favorites,    │  │  /storage    ← your files        │  │
│  │   share links)  │  │  /thumbnails ← auto-generated    │  │
│  └─────────────────┘  └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

Three services run on the Pi, all managed by systemd (auto-start on boot):

| Service | What it does |
|---------|-------------|
| `ughstorage` | The main server — handles all file operations, search, thumbnails, device info |
| `ughstorage-ble` | Bluetooth server — handles WiFi provisioning and device registration |
| `cloudflared` | Cloudflare Tunnel — securely routes traffic from `ughstorage.com` to the Pi |

### Security Model

```
┌──────────┐         ┌─────────────────┐         ┌──────────────┐
│  iPhone  │  HTTPS  │  Cloudflare     │  HTTPS  │  Pi          │
│          │────────▶│  (TLS termination│────────▶│              │
│  JWT     │◀────────│   + tunnel)     │◀────────│  JWT verify  │
│  token   │         │                 │         │  per-device  │
└──────────┘         └─────────────────┘         │  secret      │
                                                  └──────────────┘
```

- **Every request** carries a JWT token signed with a per-device secret
- **Cloudflare Tunnel** means zero open ports on your home network
- **Path traversal protection** prevents accessing files outside your storage
- **BLE provisioning** means the Pi is never exposed during setup
- **Your files stay on your NVMe drive** — they're never uploaded to any cloud server

---

## What You'll Need

### Hardware (~$100-150 one-time)

| Item | Approx. Cost | Notes |
|------|-------------|-------|
| [Raspberry Pi 5](https://www.raspberrypi.com/products/raspberry-pi-5/) | $60-80 | 4GB or 8GB RAM — either works |
| NVMe SSD (256GB - 2TB) | $25-100 | Any M.2 2230 or 2242 NVMe drive — this is your storage |
| [NVMe HAT/Base](https://pimoroni.com/nvmebase) | $15 | Connects the SSD to the Pi (Pimoroni, Geekworm, etc.) |
| USB-C Power Supply (27W) | $12 | [Official Pi 5 PSU](https://www.raspberrypi.com/products/27w-usb-c-power-supply/) recommended |
| MicroSD Card (32GB+) | $8 | For booting the OS — any brand works |

> **Tip:** Choose your NVMe SSD size based on how much storage you want. A 1TB drive gives you roughly 930GB of usable space — more than iCloud's base 200GB plan.

### Software

- An **iPhone** running iOS 17 or later
- A **computer** (Mac, Windows, or Linux) to flash the MicroSD card

---

## Setup Guide

### Step 1: Assemble the Hardware (5 min)

1. **Attach the NVMe SSD** to the NVMe HAT/Base board — it clicks into the M.2 slot
2. **Connect the HAT** to the Pi 5 — plug in the flat ribbon cable on the underside of the Pi
3. **Insert the MicroSD card** into the Pi's card slot (we'll flash it next)
4. **Don't power it on yet**

> If you bought a case, assemble it now. The NVMe drive can get warm during heavy use so a case with ventilation helps.

---

### Step 2: Flash the Operating System (10 min)

1. Download and install **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)** on your computer

2. Open Imager and select:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS Lite (64-bit) — find it under *"Raspberry Pi OS (other)"*
   - **Storage:** Your MicroSD card

3. **Before writing**, click the gear icon (⚙️) or "Edit Settings":
   - **Hostname:** `ughstorage` (or whatever you want)
   - **Enable SSH:** Yes → Use password authentication
   - **Username:** `pi`
   - **Password:** Choose something strong — you'll need this to log in
   - **WiFi:** Enter your home WiFi name and password (this is just for initial setup — the app will configure WiFi properly later)

4. Click **Write** and wait for it to finish (~5 min)

5. Put the MicroSD card back into the Pi and **plug in the power supply**

6. Wait **~2 minutes** for the Pi to boot up for the first time

---

### Step 3: Connect to the Pi (2 min)

From your computer, open a terminal (Terminal on Mac, PowerShell on Windows) and SSH in:

```bash
ssh pi@ughstorage.local
```

> **Can't connect?** Try `ssh pi@raspberrypi.local` instead. If neither works, check your router's admin page for the Pi's IP address and use `ssh pi@<IP_ADDRESS>`.

Enter the password you set in Step 2.

---

### Step 4: Set Up the NVMe SSD (5 min)

Once you're connected via SSH, run these commands to format and mount your SSD:

```bash
# Check that the Pi sees the NVMe drive
lsblk
# You should see "nvme0n1" in the list
```

```bash
# Create a partition on the drive
sudo fdisk /dev/nvme0n1
# Type: n → Enter → Enter → Enter → Enter → w
# (creates one big partition using the whole drive)
```

```bash
# Format it
sudo mkfs.ext4 /dev/nvme0n1p1
```

```bash
# Create the mount point and mount it
sudo mkdir -p /mnt/nvme
sudo mount /dev/nvme0n1p1 /mnt/nvme
sudo chown pi:pi /mnt/nvme
```

```bash
# Make it mount automatically on every boot
echo '/dev/nvme0n1p1 /mnt/nvme ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
```

```bash
# Verify — you should see your drive's full capacity
df -h /mnt/nvme
```

---

### Step 5: Install UghStorage Server (5 min)

```bash
# Download UghStorage
cd /home/pi
git clone https://github.com/hneogy/Ugh-Storage--Setup-Files.git
cd ughstorage/server

# Make the scripts executable
chmod +x setup.sh ble_setup_service.sh

# Run the setup
./setup.sh
```

This installs everything automatically:
- Python, ffmpeg, and cloudflared
- The UghStorage server software
- Storage directories on your NVMe drive
- A background service that starts automatically on boot

---

### Step 6: Install Bluetooth Setup Service (2 min)

```bash
sudo bash ble_setup_service.sh
```

This sets up the Bluetooth service that lets the iPhone app find and configure your Pi. After this runs, your Pi will start advertising as **"UghStorage-Setup"** over Bluetooth.

---

### Step 7: Start the Server

```bash
# Start the storage server
sudo systemctl start ughstorage

# Verify it's running
sudo systemctl status ughstorage
# Should show "active (running)"
```

Quick test:
```bash
curl http://localhost:8000/health
# Should return {"status": "ok", ...}
```

---

### Step 8: Download the App and Connect

1. **Download UghStorage** from the App Store
   <!-- TODO: App Store link -->
   > *Coming soon — join the waitlist at [ughstorage.com](https://ughstorage.com)*

2. **Create an account** in the app

3. **Tap "Add Device"** — the app will walk you through:

   **🔍 Find Your Pi**
   - Make sure Bluetooth is on and you're near the Pi
   - The app scans for your device and shows it as "UghStorage-Setup"
   - Tap it to connect

   **📶 Connect to WiFi**
   - The app shows WiFi networks your Pi can see
   - Select your home WiFi and enter the password
   - The Pi connects to your network

   **✅ Register**
   - The app automatically registers your Pi with your account
   - A secure Cloudflare Tunnel is provisioned — this is what makes your Pi accessible from anywhere
   - Your device gets a unique secure URL under `ughstorage.com`

4. **Done!** Start uploading files. They go straight to your Pi's NVMe drive.

---

## That's It — You're Live

Your UghStorage device is now:
- ✅ Running 24/7 on your home network
- ✅ Accessible from anywhere through `ughstorage.com`
- ✅ Encrypted end-to-end via Cloudflare Tunnel
- ✅ Automatically starting on boot/power outage
- ✅ **Your files, on your hardware, under your control**

---

## Managing Your Device

### From the App

- View device status, storage usage, and connection info in **Settings**
- Rename your device, manage WiFi, and view system info
- Factory reset from the app if you ever need to start fresh

### From SSH (Advanced)

```bash
# Check service status
sudo systemctl status ughstorage

# View server logs (live)
journalctl -u ughstorage -f

# Check disk space
df -h /mnt/nvme

# Check CPU temperature
vcgencmd measure_temp

# Restart the server
sudo systemctl restart ughstorage
```

### Updating

```bash
cd /home/pi/ughstorage
git pull
cd server
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ughstorage
```

---

## Troubleshooting

<details>
<summary><strong>"Can't find device" during Bluetooth setup</strong></summary>

- Make sure you're within ~10 feet of the Pi
- Ensure Bluetooth is enabled on your iPhone (Settings → Bluetooth)
- On the Pi, check: `sudo systemctl status ughstorage-ble`
- Restart the BLE service: `sudo systemctl restart ughstorage-ble`
</details>

<details>
<summary><strong>"Server unreachable" in the app</strong></summary>

- Check the tunnel: `sudo systemctl status cloudflared`
- Check the server: `sudo systemctl status ughstorage`
- Test locally: `curl http://localhost:8000/health`
- View logs: `journalctl -u ughstorage -f`
</details>

<details>
<summary><strong>WiFi won't connect during setup</strong></summary>

- Double-check the WiFi password
- Make sure the Pi is within range of your WiFi router
- Check NetworkManager: `nmcli device wifi list`
</details>

<details>
<summary><strong>Uploads fail or are slow</strong></summary>

- Check available disk space: `df -h /mnt/nvme`
- Maximum file size is 5GB per upload
- Make sure the Pi has good ventilation — thermal throttling slows everything
- Check server logs: `journalctl -u ughstorage -f`
</details>

<details>
<summary><strong>Pi lost power / rebooted</strong></summary>

Don't worry — everything starts back up automatically. The `ughstorage` and `cloudflared` services are configured to auto-start on boot. Just give it ~1 minute after power returns.
</details>

For in-depth diagnostics with step-by-step fixes for every error, see **[Troubleshooting Guide](docs/TROUBLESHOOTING.md)**.

Have a question not covered here? Check the **[FAQ](docs/FAQ.md)**.

---

## How Much Does It Cost?

| | iCloud (2TB) | Google One (2TB) | UghStorage (1TB) |
|---|---|---|---|
| **Year 1** | $120 | $100 | ~$130 (hardware) |
| **Year 2** | $240 | $200 | ~$15 (electricity) |
| **Year 3** | $360 | $300 | ~$15 |
| **After 5 years** | **$600** | **$500** | **~$190** |
| **Who sees your files?** | Apple | Google | **Nobody** |
| **Storage expandable?** | No | No | **Yes** (swap SSD anytime) |

Your hardware pays for itself in **about 1 year**. After that, you're saving $100+/year forever.

---

## Security

- **End-to-end encryption** via Cloudflare Tunnel — your files are encrypted in transit
- **Zero open ports** — the Pi connects outbound only, nothing is exposed on your network
- **Per-device authentication** — each Pi gets its own cryptographic secret
- **Your data stays home** — files are stored on your NVMe drive, never uploaded to the cloud
- **BLE provisioning** — initial setup happens over Bluetooth, not over the network

---

## License

MIT
