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
sudo apt-get install -y -qq python3-pip python3-venv ffmpeg

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
