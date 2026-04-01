# UghStorage — Troubleshooting Guide

A complete reference for diagnosing and fixing every issue you might encounter with UghStorage. Organized by when the problem occurs.

---

## Quick Diagnostics

Before diving into specific issues, run these commands on your Pi (via SSH) to get a snapshot of system health:

```bash
# Are all services running?
sudo systemctl status ughstorage
sudo systemctl status ughstorage-ble
sudo systemctl status cloudflared

# Is the server responding?
curl http://localhost:8000/health

# Disk space
df -h /mnt/nvme

# CPU temperature (should be under 80C)
vcgencmd measure_temp

# Memory usage
free -h

# Recent server errors
journalctl -u ughstorage --since "1 hour ago" --no-pager | grep -i error
```

---

## 1. Hardware Setup Issues

### Pi doesn't boot (no green LED activity)

**Symptoms:** Plugging in power shows only the red LED (power), no flashing green LED (activity).

**Causes & fixes:**
- **Corrupted MicroSD:** Re-flash with Raspberry Pi Imager. Make sure you selected the right device in the Imager.
- **Bad MicroSD card:** Try a different MicroSD card. Cheap cards often fail silently.
- **Insufficient power supply:** The Pi 5 needs a 27W (5V/5A) USB-C supply. Phone chargers are usually too weak. Use the [official Pi 5 PSU](https://www.raspberrypi.com/products/27w-usb-c-power-supply/).
- **Loose MicroSD:** Push the card firmly into the slot until it clicks.

### Pi boots but NVMe SSD not detected

**Symptoms:** `lsblk` doesn't show `nvme0n1`.

**Causes & fixes:**
- **Loose ribbon cable:** Power off, disconnect the NVMe HAT, and reconnect the flat ribbon cable. It should click firmly into both connectors.
- **Incompatible drive:** Not all M.2 drives work with all HATs. Check your HAT's compatibility list. Most 2230 and 2242 drives work.
- **PCIe not enabled:** Run `sudo rpi-update` and reboot. Some Pi 5 firmware versions need an update for NVMe.
- **Try `lspci`:** If this shows the NVMe controller but `lsblk` doesn't show the drive, the drive may be defective.

### fdisk: "Unable to open /dev/nvme0n1"

**Causes & fixes:**
- **Wrong device name:** Run `lsblk` to find the correct device. It might be `/dev/nvme0n1` (no partition number) for the whole disk.
- **Permission denied:** Use `sudo fdisk /dev/nvme0n1`.
- **Drive already has partitions:** If you see `nvme0n1p1` already, you can skip fdisk and go straight to formatting with `mkfs.ext4`.

### mkfs.ext4: "Device is busy"

**Causes & fixes:**
- **Drive is mounted:** Run `sudo umount /dev/nvme0n1p1` first, then format.
- **Drive is in use by another process:** Run `sudo fuser -v /dev/nvme0n1p1` to see what's using it.

---

## 2. Setup Script Issues

### setup.sh: "apt-get update failed"

**Symptoms:** The setup script fails at step 1 with package manager errors.

**Causes & fixes:**
- **No internet:** Make sure the Pi is connected to WiFi or Ethernet. Test with `ping google.com`.
- **DNS issues:** Try `echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf` then retry.
- **Repository errors:** Run `sudo apt-get update` manually to see the specific error. Often a temporary server issue — wait 10 minutes and retry.

### setup.sh: "pip install failed"

**Symptoms:** Python package installation fails during step 3.

**Causes & fixes:**
- **Disk full:** Check with `df -h`. The MicroSD needs at least 1GB free for Python packages.
- **Network timeout:** Retry. PyPI can be slow sometimes.
- **Python version mismatch:** UghStorage requires Python 3.9+. Check with `python3 --version`. Raspberry Pi OS Lite (64-bit) ships with Python 3.11+ which is fine.
- **Specific package fails (Pillow):** Pillow needs image libraries. Run:
  ```bash
  sudo apt-get install -y libjpeg-dev libpng-dev libtiff-dev
  ```
  Then retry `pip install -r requirements.txt`.

### setup.sh: "cloudflared download failed"

**Symptoms:** Can't download the cloudflared binary.

**Causes & fixes:**
- **Network issue:** Test with `curl -I https://github.com`. If this fails, fix your internet connection first.
- **Architecture mismatch:** The script downloads the ARM64 version. If you're on a 32-bit OS (don't be), you need the ARM version instead. Use 64-bit OS as recommended.
- **Manual install:** Download manually:
  ```bash
  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /tmp/cloudflared
  sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
  sudo chmod +x /usr/local/bin/cloudflared
  cloudflared --version
  ```

### ble_setup_service.sh: "This script must be run as root"

**Fix:** Run with `sudo`:
```bash
sudo bash ble_setup_service.sh
```

### ble_setup_service.sh: "NetworkManager won't start"

**Symptoms:** Step fails with NetworkManager errors.

**Causes & fixes:**
- **Conflict with dhcpcd:** The script tries to disable dhcpcd. If it fails:
  ```bash
  sudo systemctl stop dhcpcd
  sudo systemctl disable dhcpcd
  sudo systemctl restart NetworkManager
  ```
- **WiFi firmware missing:** Some Pi models need firmware. Run:
  ```bash
  sudo apt-get install -y firmware-brcm80211
  ```
- **Verify manually:**
  ```bash
  nmcli general status
  # Should show "connected" or "disconnected" — not errors
  ```

### ble_setup_service.sh: "Bluetooth adapter not found"

**Symptoms:** `hciconfig` shows nothing, or the BLE service fails to start.

**Causes & fixes:**
- **Bluetooth not enabled in OS:**
  ```bash
  sudo systemctl enable bluetooth
  sudo systemctl start bluetooth
  sudo hciconfig hci0 up
  ```
- **Hardware issue:** Check `dmesg | grep -i bluetooth` for firmware errors. On some Pi units, a reboot fixes Bluetooth initialization.
- **Blocked by rfkill:**
  ```bash
  sudo rfkill list
  # If Bluetooth is "Soft blocked" or "Hard blocked":
  sudo rfkill unblock bluetooth
  ```

---

## 3. Bluetooth (BLE) Setup Issues

### App can't find the Pi during setup

**Symptoms:** The "Find Device" screen shows no devices.

**Check on the Pi:**
```bash
# Is the BLE service running?
sudo systemctl status ughstorage-ble

# Check BLE logs for errors
journalctl -u ughstorage-ble --since "5 minutes ago" --no-pager

# Is the Bluetooth adapter up?
hciconfig hci0
# Should show "UP RUNNING"

# Is it advertising?
sudo btmgmt info
# Should show "current settings: powered le bondable advertising"
```

**Fixes:**
- **Restart the BLE service:**
  ```bash
  sudo systemctl restart ughstorage-ble
  ```
- **Move closer:** BLE range is ~30 feet, but walls and interference reduce it. Get within ~10 feet.
- **Toggle iPhone Bluetooth:** Turn Bluetooth off and on in iPhone Settings.
- **Kill competing BLE connections:** If other apps are using BLE, they might interfere. Close other apps.
- **Check for dbus-next:**
  ```bash
  python3 -c "import dbus_next; print('OK')"
  # If this fails: sudo pip3 install --break-system-packages dbus-next
  ```

### BLE service crashes with "D-Bus connection refused"

**Symptoms:** `journalctl -u ughstorage-ble` shows D-Bus errors.

**Causes & fixes:**
- **D-Bus not running:**
  ```bash
  sudo systemctl status dbus
  sudo systemctl restart dbus
  sudo systemctl restart ughstorage-ble
  ```
- **BlueZ not running:**
  ```bash
  sudo systemctl restart bluetooth
  # Wait 3 seconds
  sudo systemctl restart ughstorage-ble
  ```
- **Permission issues:** The BLE service runs as root. If you changed this, ensure it has access to `/var/run/dbus/system_bus_socket`.

### BLE service: "dbus-next library not found"

**Fix:**
```bash
sudo pip3 install --break-system-packages dbus-next
sudo systemctl restart ughstorage-ble
```

### BLE service: "NetworkManager (nmcli) is not available"

**Symptoms:** BLE service starts but WiFi operations fail. Logs show the warning.

**Fix:**
```bash
sudo apt-get install -y network-manager
sudo systemctl enable NetworkManager
sudo systemctl start NetworkManager
sudo systemctl restart ughstorage-ble
```

---

## 4. WiFi Connection Issues

### WiFi scan returns no networks

**Check:**
```bash
# Is the WiFi adapter working?
nmcli device status
# wlan0 should show "wifi" type

# Manual scan
nmcli device wifi list
```

**Fixes:**
- **WiFi adapter down:**
  ```bash
  sudo nmcli device set wlan0 managed yes
  nmcli radio wifi on
  ```
- **Interference/range:** Move the Pi closer to the router.
- **5GHz network not visible:** The Pi 5 supports both 2.4GHz and 5GHz, but some regions require specific country settings:
  ```bash
  sudo raspi-config
  # → Localisation Options → WLAN Country → Set your country
  ```

### "Incorrect password" when connecting to WiFi

**Causes & fixes:**
- **Wrong password:** Double-check the password. WiFi passwords are case-sensitive.
- **Special characters:** If your password has special characters (`$`, `"`, `\`), they might not transmit correctly over BLE. Try changing your WiFi password temporarily to alphanumeric only.
- **WPA3:** If your router uses WPA3 only, the Pi may have trouble. Try switching the router to WPA2/WPA3 mixed mode.

### "Connection timed out" during WiFi setup

**Causes & fixes:**
- **Too far from router:** Move the Pi closer.
- **Router MAC filtering:** If you have MAC address filtering enabled on your router, add the Pi's WiFi MAC address:
  ```bash
  ip link show wlan0
  # Look for "link/ether XX:XX:XX:XX:XX:XX"
  ```
- **Too many connected devices:** Some consumer routers limit connections. Disconnect a device or check your router settings.
- **Network congestion:** Try again in a minute.

### WiFi connects but Pi can't reach the internet

**Check:**
```bash
nmcli device show wlan0 | grep IP4.ADDRESS
# Should show an IP address

ping -c 3 8.8.8.8
# Should get responses

ping -c 3 google.com
# If this fails but the above works, it's a DNS issue
```

**DNS fix:**
```bash
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
```

---

## 5. Device Registration Issues

### Registration fails: "register-device failed (HTTP 401)"

**Symptoms:** The app shows registration error during setup.

**Causes:**
- **Expired app token:** Sign out of the app and sign back in, then retry setup.
- **Account issue:** Make sure your UghStorage account is active.

### Registration fails: "register-device failed (HTTP 500)"

**Causes:**
- **Server-side issue:** This is a problem with the UghStorage backend. Wait a few minutes and retry.
- **Check the Pi's internet:**
  ```bash
  curl -I https://ooadxfhisydhcgktaemt.supabase.co
  # Should return HTTP 200 or 301
  ```

### Registration fails: "cloudflared binary not found"

**Fix:** The setup script didn't install cloudflared. Install manually:
```bash
sudo curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
```
Then retry registration from the app.

### Registration succeeds but tunnel won't start

**Check:**
```bash
sudo systemctl status cloudflared
journalctl -u cloudflared --since "5 minutes ago" --no-pager
```

**Common issues:**
- **"Permission denied" writing service file:**
  ```bash
  # Check if the service file exists
  ls -la /etc/systemd/system/cloudflared.service
  # If not, the registration should have created it. Check BLE logs:
  journalctl -u ughstorage-ble --since "10 minutes ago" --no-pager | grep -i cloud
  ```
- **Invalid tunnel token:** Factory reset from the app and re-register. The token may have been corrupted during BLE transfer.
- **Network firewall:** Some corporate or hotel networks block outbound connections to Cloudflare. Use a regular home network for setup.

### Registration succeeds but app says "Server unreachable"

**Wait 30-60 seconds.** The Cloudflare Tunnel takes a moment to propagate. If it still doesn't work after 2 minutes:

```bash
# Is the tunnel running?
sudo systemctl status cloudflared

# Is the server running?
sudo systemctl status ughstorage

# Can you reach the server locally?
curl http://localhost:8000/health
```

If the server works locally but not through the tunnel, restart cloudflared:
```bash
sudo systemctl restart cloudflared
```

---

## 6. Server Issues (After Setup)

### Server won't start: "Address already in use"

**Symptoms:** `systemctl status ughstorage` shows the error.

**Fix:** Something else is using port 8000:
```bash
sudo lsof -i :8000
# Kill whatever process is using it, then restart:
sudo systemctl restart ughstorage
```

### Server won't start: "ModuleNotFoundError"

**Symptoms:** Missing Python module in the logs.

**Fix:**
```bash
cd /home/pi/ughstorage/server
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ughstorage
```

### Server crashes periodically

**Check logs for the crash reason:**
```bash
journalctl -u ughstorage --since "24 hours ago" --no-pager | grep -i "error\|exception\|killed"
```

**Common causes:**
- **Out of memory (OOM killed):** Large file uploads can use significant RAM. Check with `dmesg | grep -i oom`. Fix: add swap space:
  ```bash
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
  ```
- **Disk full:** Check `df -h /mnt/nvme`. Delete old files or empty trash.
- **Database corruption:** Rare, but can happen after power loss during a write. Fix:
  ```bash
  cd /home/pi/ughstorage/server
  source venv/bin/activate
  python3 -c "import database; import asyncio; asyncio.run(database.init_db())"
  sudo systemctl restart ughstorage
  ```

### "Device not registered. Complete BLE setup first." (HTTP 503)

**Symptoms:** Every API request returns 503.

**Cause:** The `DEVICE_ID` and `DEVICE_SHARED_SECRET` in `.env` are empty. The device hasn't been registered yet, or credentials were cleared.

**Fix:** Register the device through the app (Settings → Add Device). If you've already registered and this is happening unexpectedly:
```bash
cat /home/pi/ughstorage/server/.env | grep DEVICE
# UGHSTORAGE_DEVICE_ID should have a value
# UGHSTORAGE_DEVICE_SHARED_SECRET should have a value
```
If they're empty, you need to factory reset and re-register.

### "Token has expired" (HTTP 401)

**Cause:** The JWT token from the app has expired.

**Fix:** This is handled automatically by the app — it refreshes tokens. If you're seeing this persistently, sign out of the app and sign back in.

### "File exceeds maximum size" (HTTP 413)

**Default limit:** 5 GB per file.

**To increase:**
```bash
# Edit .env
nano /home/pi/ughstorage/server/.env
# Add or edit: UGHSTORAGE_MAX_UPLOAD_SIZE=10737418240  (10 GB)
sudo systemctl restart ughstorage
```

---

## 7. Upload & Download Issues

### Uploads are very slow

**Causes:**
- **Slow home internet upload speed:** Test at [speedtest.net](https://speedtest.net). Your upload speed is the bottleneck for remote access. 10 Mbps upload ≈ 1.2 MB/s file transfer.
- **Pi is thermal throttling:** Check `vcgencmd measure_temp`. If over 80C, add a heatsink or fan.
- **WiFi interference:** If the Pi is on WiFi, try Ethernet for faster local transfers.
- **Large thumbnail generation:** When uploading many photos/videos, thumbnail generation runs in the background and uses CPU. This is temporary.

### Upload fails with "500 Internal Server Error"

**Check server logs:**
```bash
journalctl -u ughstorage --since "5 minutes ago" --no-pager | tail -30
```

**Common causes:**
- **Disk full:**
  ```bash
  df -h /mnt/nvme
  # If less than 100MB free, delete files or empty trash
  ```
- **Storage directory permissions:**
  ```bash
  ls -la /mnt/nvme/storage/
  # Owner should be pi:pi
  # If not: sudo chown -R pi:pi /mnt/nvme/storage /mnt/nvme/thumbnails
  ```
- **Database locked:** Restart the server:
  ```bash
  sudo systemctl restart ughstorage
  ```

### Downloads are slow or timeout

**Causes:**
- **Home internet upload speed:** Downloads from your Pi use your home upload bandwidth.
- **Large file + slow connection:** Very large files may timeout on slow connections. Try downloading on WiFi.
- **Tunnel overloaded:** Restart the tunnel:
  ```bash
  sudo systemctl restart cloudflared
  ```

### Thumbnails not generating

**Check:**
```bash
# Is ffmpeg installed?
ffmpeg -version

# Check thumbnail directory
ls /mnt/nvme/thumbnails/

# Check for errors
journalctl -u ughstorage --since "1 hour ago" --no-pager | grep -i thumbnail
```

**Fix for images:**
```bash
# Pillow needs image libraries
sudo apt-get install -y libjpeg-dev libpng-dev
cd /home/pi/ughstorage/server
source venv/bin/activate
pip install --force-reinstall Pillow
sudo systemctl restart ughstorage
```

**Fix for videos:**
```bash
sudo apt-get install -y ffmpeg
sudo systemctl restart ughstorage
```

---

## 8. Cloudflare Tunnel Issues

### Tunnel won't start after registration

**Check:**
```bash
sudo systemctl status cloudflared
journalctl -u cloudflared -f
```

**Fixes:**
- **Invalid token:** Factory reset from the app and re-register.
- **Missing binary:**
  ```bash
  ls -la /usr/local/bin/cloudflared
  # If missing, reinstall:
  sudo curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared
  sudo chmod +x /usr/local/bin/cloudflared
  sudo systemctl restart cloudflared
  ```
- **Service file missing:**
  ```bash
  ls -la /etc/systemd/system/cloudflared.service
  # If missing, the registration didn't complete properly. Factory reset and re-register.
  ```

### Tunnel keeps disconnecting and reconnecting

**Symptoms:** App intermittently shows "Server unreachable" then works again.

**Causes:**
- **Unstable internet:** Check your internet stability. Run `ping -c 100 8.8.8.8` and look for packet loss.
- **ISP issues:** Some ISPs drop long-lived connections. Cloudflared handles reconnection automatically, but frequent drops indicate ISP problems.
- **DNS issues on the Pi:**
  ```bash
  echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
  sudo systemctl restart cloudflared
  ```

### "ERR_CONNECTION_TIMED_OUT" in browser when visiting device URL

**Causes:**
- **Tunnel not running:** `sudo systemctl status cloudflared`
- **Server not running:** `sudo systemctl status ughstorage`
- **Pi offline:** Check if the Pi is powered on and connected to WiFi.
- **Tunnel token revoked:** Factory reset and re-register.

---

## 9. App Issues

### App says "Server unreachable" but everything worked before

**Quick checks (in order):**
1. **Is your Pi plugged in and powered?** Check the LEDs.
2. **Is your home internet working?** Check from another device.
3. **SSH into the Pi** and run the quick diagnostics at the top of this guide.
4. **Restart everything:**
   ```bash
   sudo systemctl restart ughstorage
   sudo systemctl restart cloudflared
   ```
5. **Still broken?** Check `journalctl -u cloudflared -f` for tunnel errors.

### App login fails

- **"Invalid credentials":** Check your email and password. Passwords are case-sensitive.
- **"Network error":** Check your phone's internet connection. The login goes through `ughstorage.com`, not your Pi.
- **Account locked:** Too many failed attempts. Wait 15 minutes and try again.

### Files appear in the app but won't download

**Cause:** The file exists in the database but was deleted from the NVMe drive, or the drive became unmounted.

**Check on Pi:**
```bash
# Is the NVMe mounted?
df -h /mnt/nvme
# If not mounted:
sudo mount /dev/nvme0n1p1 /mnt/nvme

# Does the file exist on disk?
ls /mnt/nvme/storage/
```

### App is slow to load file lists

**Causes:**
- **Many files (1000+):** Loading large file lists takes time over a tunnel. This is normal.
- **Thumbnail generation backlog:** If you just uploaded many files, thumbnails are still being generated. This uses CPU and slows other operations temporarily.
- **Slow home internet:** Your upload speed affects how fast the Pi can respond.

---

## 10. Factory Reset & Recovery

### How to factory reset from the app

Settings → Danger Zone → Factory Reset

This will:
1. Delete the device record from UghStorage backend
2. Stop the Cloudflare Tunnel
3. Clear device credentials from `.env`
4. Restart the BLE service (Pi becomes discoverable again)

Your **files are NOT deleted** during factory reset. Only the device registration is cleared.

### How to factory reset from SSH

```bash
# Stop services
sudo systemctl stop cloudflared
sudo systemctl stop ughstorage

# Clear device credentials
cd /home/pi/ughstorage/server
sed -i 's/^UGHSTORAGE_DEVICE_ID=.*/UGHSTORAGE_DEVICE_ID=/' .env
sed -i 's/^UGHSTORAGE_DEVICE_SHARED_SECRET=.*/UGHSTORAGE_DEVICE_SHARED_SECRET=/' .env
sed -i '/^UGHSTORAGE_TUNNEL_TOKEN=/d' .env

# Restart services
sudo systemctl start ughstorage
sudo systemctl restart ughstorage-ble

# Pi is now discoverable for re-registration
```

### How to completely start over (nuclear option)

If everything is broken and you want a clean slate:

```bash
# On the Pi:
cd /home/pi
rm -rf ughstorage

# Re-clone and re-setup
git clone https://github.com/hneogy/ughstorage.git
cd ughstorage/server
chmod +x setup.sh ble_setup_service.sh
./setup.sh
sudo bash ble_setup_service.sh
sudo systemctl start ughstorage
```

Your files on `/mnt/nvme/storage/` are **untouched** by this process. The new server will see them if the database is still intact. If the database is corrupted, the files are still on disk but won't appear in the app until re-indexed (not yet automated — you'd need to re-upload or manually rebuild the database).

### How to wipe all files and start completely fresh

**Warning: This deletes all your files permanently.**

```bash
# Stop the server
sudo systemctl stop ughstorage

# Delete everything
rm -rf /mnt/nvme/storage/*
rm -rf /mnt/nvme/thumbnails/*
rm -f /mnt/nvme/ughstorage.db

# Restart
sudo systemctl start ughstorage
# The server will create a fresh database on startup
```

---

## 11. Service Management Reference

### Start / stop / restart services

```bash
# Main server
sudo systemctl start ughstorage
sudo systemctl stop ughstorage
sudo systemctl restart ughstorage

# BLE setup service
sudo systemctl start ughstorage-ble
sudo systemctl stop ughstorage-ble
sudo systemctl restart ughstorage-ble

# Cloudflare tunnel
sudo systemctl start cloudflared
sudo systemctl stop cloudflared
sudo systemctl restart cloudflared
```

### View service logs

```bash
# Live logs (Ctrl+C to stop)
journalctl -u ughstorage -f
journalctl -u ughstorage-ble -f
journalctl -u cloudflared -f

# Last 100 lines
journalctl -u ughstorage -n 100 --no-pager

# Errors only
journalctl -u ughstorage -p err --since "24 hours ago" --no-pager

# Since last boot
journalctl -u ughstorage -b --no-pager
```

### Check if services are enabled (auto-start on boot)

```bash
systemctl is-enabled ughstorage        # Should be "enabled"
systemctl is-enabled ughstorage-ble    # Should be "enabled"
systemctl is-enabled cloudflared       # Should be "enabled"
```

### Service file locations

```
/etc/systemd/system/ughstorage.service        # Main server
/etc/systemd/system/ughstorage-ble.service     # BLE provisioning
/etc/systemd/system/cloudflared.service        # Tunnel (created during registration)
```

---

## 12. Common Error Messages Reference

| Error | Where | Meaning | Fix |
|-------|-------|---------|-----|
| "Device not registered. Complete BLE setup first." | API (503) | No device credentials in `.env` | Register through the app |
| "Token has expired" | API (401) | JWT expired | Sign out and back into the app |
| "Invalid token" | API (401) | Wrong or corrupted JWT | Sign out and back into the app |
| "File not found" | API (404) | File ID doesn't exist in database | File was deleted or never uploaded |
| "File missing from storage" | API (404) | Database has record but file missing from disk | NVMe may have unmounted — check `df -h /mnt/nvme` |
| "File exceeds maximum size" | API (413) | File larger than 5GB limit | Increase limit in `.env` or upload smaller file |
| "Invalid path" | API (400) | Path traversal attempt or invalid characters | Use normal folder paths, no `..` |
| "Share link has expired" | API (410) | Share link past its expiry time | Create a new share link |
| "nmcli not found" | BLE service | NetworkManager not installed | `sudo apt-get install -y network-manager` |
| "dbus-next library not found" | BLE service | Missing Python package | `sudo pip3 install --break-system-packages dbus-next` |
| "cloudflared binary not found" | Registration | cloudflared not installed | Run `setup.sh` or install manually |
| "register-device failed (HTTP 401)" | Registration | Bad or expired user token | Sign out/in in the app, retry |
| "register-device failed (HTTP 500)" | Registration | Backend server error | Wait a few minutes, retry |
| "Incorrect password" | WiFi | Wrong WiFi password | Re-enter the correct password |
| "Connection timed out" | WiFi | Can't reach the WiFi network | Move Pi closer to router |

---

## Still Stuck?

If none of the above solves your problem:

1. **Gather logs:**
   ```bash
   journalctl -u ughstorage --since "1 hour ago" --no-pager > /tmp/ugh-server.log
   journalctl -u ughstorage-ble --since "1 hour ago" --no-pager > /tmp/ugh-ble.log
   journalctl -u cloudflared --since "1 hour ago" --no-pager > /tmp/ugh-tunnel.log
   ```

2. **Open an issue** on [GitHub](https://github.com/hneogy/ughstorage/issues) with:
   - What you were doing when the problem occurred
   - The exact error message
   - Relevant log output from the commands above
   - Your Pi model and OS version (`cat /etc/os-release`)
