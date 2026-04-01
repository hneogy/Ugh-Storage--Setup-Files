#!/usr/bin/env bash
#
# ble_setup_service.sh
#
# Installs dependencies, configures Bluetooth, and creates a systemd service
# for the UghStorage BLE WiFi provisioning server on Raspberry Pi 5 (Pi OS Lite).
#
# Usage:
#   sudo bash ble_setup_service.sh [--uninstall]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ughstorage-ble"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="/usr/bin/python3"
BLE_SCRIPT="${SCRIPT_DIR}/ble_setup.py"
CONFIG_DIR="/etc/ughstorage"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()   { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
error() { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)."
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

uninstall() {
    log "Uninstalling ${SERVICE_NAME} service..."
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    log "Service removed. Bluetooth packages were NOT removed."
    exit 0
}

# ---------------------------------------------------------------------------
# Install dependencies
# ---------------------------------------------------------------------------

install_dependencies() {
    log "Updating package lists..."
    apt-get update -qq

    log "Installing Bluetooth and NetworkManager packages..."
    apt-get install -y -qq \
        bluez \
        bluetooth \
        pi-bluetooth \
        python3-pip \
        python3-venv \
        network-manager \
        libdbus-1-dev \
        libglib2.0-dev

    log "Installing Python dependencies..."
    pip3 install --break-system-packages dbus-next 2>/dev/null \
        || pip3 install dbus-next

    log "Dependencies installed."
}

# ---------------------------------------------------------------------------
# Configure NetworkManager
# ---------------------------------------------------------------------------

configure_networkmanager() {
    log "Configuring NetworkManager..."

    # Ensure NetworkManager manages WiFi (not dhcpcd)
    if systemctl is-active --quiet dhcpcd 2>/dev/null; then
        log "Disabling dhcpcd in favor of NetworkManager..."
        systemctl stop dhcpcd || true
        systemctl disable dhcpcd || true
    fi

    # Make sure NetworkManager is not blocked from managing wlan0
    NM_CONF="/etc/NetworkManager/NetworkManager.conf"
    if [[ -f "${NM_CONF}" ]]; then
        # Ensure wifi is managed
        if ! grep -q "^\[ifupdown\]" "${NM_CONF}" 2>/dev/null; then
            cat >> "${NM_CONF}" <<'NMEOF'

[ifupdown]
managed=true
NMEOF
        fi
    fi

    # Prevent wpa_supplicant from conflicting
    # NetworkManager will handle wpa_supplicant internally
    systemctl stop wpa_supplicant 2>/dev/null || true
    systemctl disable wpa_supplicant 2>/dev/null || true

    systemctl enable NetworkManager
    systemctl restart NetworkManager

    # Wait for NM to come up
    sleep 2

    if nmcli general status >/dev/null 2>&1; then
        log "NetworkManager is active."
    else
        warn "NetworkManager may not be fully started yet."
    fi
}

# ---------------------------------------------------------------------------
# Configure Bluetooth
# ---------------------------------------------------------------------------

configure_bluetooth() {
    log "Configuring Bluetooth..."

    systemctl enable bluetooth
    systemctl start bluetooth

    # Wait for bluetooth to be ready
    sleep 2

    # Enable the adapter
    hciconfig hci0 up 2>/dev/null || true

    # Make discoverable with no timeout (persists until reboot;
    # the BLE service will set this via D-Bus on every start).
    bluetoothctl <<'BTEOF' 2>/dev/null || true
power on
discoverable on
discoverable-timeout 0
pairable on
BTEOF

    # Allow BLE advertising as non-root (optional, the service runs as root)
    # Set the adapter to support LE
    hciconfig hci0 leadv 3 2>/dev/null || true

    log "Bluetooth configured."
}

# ---------------------------------------------------------------------------
# Create config directory
# ---------------------------------------------------------------------------

create_config_dir() {
    mkdir -p "${CONFIG_DIR}"
    # Create placeholder files if they don't exist
    if [[ ! -f "${CONFIG_DIR}/tunnel_url" ]]; then
        echo "" > "${CONFIG_DIR}/tunnel_url"
    fi
    if [[ ! -f "${CONFIG_DIR}/version" ]]; then
        echo "1.0.0" > "${CONFIG_DIR}/version"
    fi
    log "Config directory ready at ${CONFIG_DIR}"
}

# ---------------------------------------------------------------------------
# Create systemd service
# ---------------------------------------------------------------------------

create_service() {
    log "Creating systemd service: ${SERVICE_NAME}..."

    cat > "${SERVICE_FILE}" <<SVCEOF
[Unit]
Description=UghStorage BLE WiFi Provisioning Service
After=bluetooth.target network-pre.target
Wants=bluetooth.target

[Service]
Type=simple
ExecStartPre=/usr/bin/sleep 3
ExecStart=${PYTHON_BIN} ${BLE_SCRIPT}
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Security hardening
ProtectSystem=full
NoNewPrivileges=false
PrivateTmp=true

# Bluetooth needs access to dbus and hci devices
SupplementaryGroups=bluetooth

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl start "${SERVICE_NAME}"

    log "Service created and started."
    log "Check status with: systemctl status ${SERVICE_NAME}"
    log "View logs with:    journalctl -u ${SERVICE_NAME} -f"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    require_root

    if [[ "${1:-}" == "--uninstall" ]]; then
        uninstall
    fi

    log "=========================================="
    log " UghStorage BLE Setup Installer"
    log "=========================================="

    # Verify the BLE script exists
    if [[ ! -f "${BLE_SCRIPT}" ]]; then
        error "BLE script not found at ${BLE_SCRIPT}"
    fi

    install_dependencies
    configure_networkmanager
    configure_bluetooth
    create_config_dir
    create_service

    log "=========================================="
    log " Installation complete!"
    log ""
    log " The BLE service is now running and will"
    log " start automatically on boot."
    log ""
    log " Service:  ${SERVICE_NAME}"
    log " BLE name: UghStorage-Setup"
    log "=========================================="
}

main "$@"
