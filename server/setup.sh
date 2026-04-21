#!/usr/bin/env bash
# UghStorage setup script for Raspberry Pi 5 (multi-tenant).
# Run as a regular user (uses sudo where needed).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
STORAGE_DIR="/mnt/nvme/storage"
THUMBNAIL_DIR="/mnt/nvme/thumbnails"
SERVICE_NAME="ughstorage"
ENV_FILE="$SCRIPT_DIR/.env"

echo "============================================"
echo "  UghStorage Setup (v2 - Multi-Tenant)"
echo "============================================"
echo

# --- System dependencies ---
echo "[1/6] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv ffmpeg git

# --- OTA state directory ---
# Persists update state across service restarts and reboots. Lives outside
# the repo so `git checkout` during an update never touches it.
sudo mkdir -p /var/lib/ughstorage
sudo chown "$(whoami):$(whoami)" /var/lib/ughstorage

# --- Passwordless sudo for the single command the update script needs ---
# Scoped tight: only `systemctl restart ughstorage` without a password.
# Everything else still requires sudo. Safer than `ALL=(ALL) NOPASSWD: ALL`.
SUDOERS_FILE="/etc/sudoers.d/ughstorage-update"
# Scoped tight: only `systemctl restart` / `systemctl start` / `systemctl stop`
# / `systemctl enable` / `systemctl disable` for units whose names start with
# `ughstorage` or `ugh-module-*`. Everything else still requires a password.
# This lets module install scripts set up their own systemd units without
# handing them a general root shell.
if [ ! -f "$SUDOERS_FILE" ]; then
    sudo tee "$SUDOERS_FILE" > /dev/null <<SUDOERS
$(whoami) ALL=(root) NOPASSWD: /bin/systemctl restart ughstorage, \\
    /bin/systemctl restart ughstorage.service, \\
    /bin/systemctl start ugh-module-*, \\
    /bin/systemctl stop ugh-module-*, \\
    /bin/systemctl restart ugh-module-*, \\
    /bin/systemctl enable ugh-module-*, \\
    /bin/systemctl disable ugh-module-*, \\
    /bin/systemctl daemon-reload, \\
    /usr/bin/tee /etc/systemd/system/ugh-module-*.service, \\
    /bin/rm /etc/systemd/system/ugh-module-*.service
SUDOERS
    sudo chmod 0440 "$SUDOERS_FILE"
    echo "  Added module / update sudoers rules."
fi

# --- Docker (used by media modules: music / photos / video) ---
# Storage runs natively (Python venv); media modules ship as upstream Docker
# images so we don't carry their build-from-source maintenance burden.
echo "[2a/6] Installing Docker..."
if ! command -v docker &>/dev/null; then
    # Use Docker's convenience installer — official, idempotent, supports arm64.
    curl -fsSL https://get.docker.com | sudo sh
    sudo systemctl enable --now docker
    # Add the current user to the docker group so module install scripts
    # don't need sudo for `docker` invocations.
    sudo usermod -aG docker "$(whoami)"
    echo "  Docker installed: $(docker --version)"
    echo "  NOTE: log out and back in (or run 'newgrp docker') for group membership to take effect."
else
    echo "  Docker already installed: $(docker --version)"
fi

# --- Cloudflared ---
echo "[2/6] Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    sudo curl -L "$CLOUDFLARED_URL" -o /usr/local/bin/cloudflared
    sudo chmod +x /usr/local/bin/cloudflared
    echo "  cloudflared installed: $(cloudflared --version)"
else
    echo "  cloudflared already installed: $(cloudflared --version)"
fi

# --- Virtual environment ---
echo "[3/6] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q
echo "  Installed Python packages."

# --- Storage directories ---
echo "[4/6] Creating storage directories..."
sudo mkdir -p "$STORAGE_DIR" "$THUMBNAIL_DIR"
sudo chown "$(whoami):$(whoami)" "$STORAGE_DIR" "$THUMBNAIL_DIR"
echo "  $STORAGE_DIR"
echo "  $THUMBNAIL_DIR"

# --- Module data layout ---
# Reserved for future media modules (sprints c+). Empty today; their install
# scripts will populate them. Created up-front so module installs don't have
# to deal with permissions on a missing parent. Distinct from $STORAGE_DIR
# (the user's bulk file storage) on purpose — keeps "user files" and
# "service data" obviously separate when debugging.
UGH_ROOT="/mnt/nvme/ughstorage"
# Module-data dirs match each module's install-script DATA_DIR so the
# "installed?" sentinel in modules.py lines up with where install_*.sh
# actually writes state.
sudo mkdir -p \
    "$UGH_ROOT/media/music" \
    "$UGH_ROOT/media/photos" \
    "$UGH_ROOT/media/video" \
    "$UGH_ROOT/module-data/music" \
    "$UGH_ROOT/module-data/immich" \
    "$UGH_ROOT/module-data/jellyfin"
sudo chown -R "$(whoami):$(whoami)" "$UGH_ROOT"
echo "  $UGH_ROOT/{media,module-data}/ (reserved for future modules)"

# --- Environment file ---
echo "[5/6] Configuring environment..."
if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists -- skipping env setup."
    echo "  Delete $ENV_FILE and re-run to reconfigure."
else
    cat > "$ENV_FILE" <<EOF
# UghStorage environment configuration
# Supabase connection (pre-configured)
UGHSTORAGE_SUPABASE_URL=https://ooadxfhisydhcgktaemt.supabase.co
UGHSTORAGE_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9vYWR4Zmhpc3lkaGNna3RhZW10Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ3NjI2MzcsImV4cCI6MjA5MDMzODYzN30.iXkNv78ZICv4b2KY3oAnFPiwpvf7Oogq3RWy0anrRmM

# Device identity (populated automatically during BLE registration)
UGHSTORAGE_DEVICE_ID=
UGHSTORAGE_DEVICE_SHARED_SECRET=

# Storage paths
UGHSTORAGE_STORAGE_ROOT=$STORAGE_DIR
UGHSTORAGE_THUMBNAIL_ROOT=$THUMBNAIL_DIR
EOF

    echo "  Environment saved to $ENV_FILE"
    echo "  NOTE: DEVICE_ID and DEVICE_SHARED_SECRET will be set during BLE registration."
fi

# --- Systemd service ---
echo "[6/6] Creating systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=UghStorage personal cloud storage
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  Service installed: $SERVICE_NAME"

# --- Done ---
echo
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo
echo "  Next steps:"
echo
echo "  1. Start the server:"
echo "       sudo systemctl start $SERVICE_NAME"
echo "       sudo systemctl status $SERVICE_NAME"
echo
echo "  2. Start the BLE setup service:"
echo "       sudo systemctl start ughstorage-ble"
echo
echo "  3. Open the iOS app and use Bluetooth to:"
echo "       a) Configure WiFi"
echo "       b) Register the device (sends your Supabase token)"
echo "       c) The device will auto-provision a tunnel and connect to the cloud"
echo
echo "  The device will be fully operational once BLE registration completes."
echo
