"""
WiFi management module for Raspberry Pi using NetworkManager (nmcli).

Provides functions to scan networks, connect, check status, disconnect,
and forget saved networks. Requires NetworkManager to be installed and active.
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class WiFiNetwork:
    ssid: str
    signal: int
    security: str
    in_use: bool = False


@dataclass
class WiFiStatus:
    connected: bool
    ssid: Optional[str] = None
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    frequency: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _run_nmcli(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run an nmcli command and return the result."""
    cmd = ["nmcli"] + args
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("nmcli returned %d: %s", result.returncode, result.stderr.strip())
        return result
    except subprocess.TimeoutExpired:
        logger.error("nmcli command timed out: %s", " ".join(cmd))
        raise
    except FileNotFoundError:
        logger.error("nmcli not found. Is NetworkManager installed?")
        raise RuntimeError("NetworkManager (nmcli) is not installed. Run ble_setup_service.sh first.")


def check_nmcli_available() -> bool:
    """Check if nmcli is available and NetworkManager is running."""
    try:
        result = _run_nmcli(["general", "status"], timeout=5)
        return result.returncode == 0
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def scan_networks() -> list[dict]:
    """
    Scan for available WiFi networks.

    Returns a list of dicts with keys: ssid, signal, security, in_use.
    Deduplicates by SSID, keeping the strongest signal for each.
    """
    # Force a rescan first
    _run_nmcli(["device", "wifi", "rescan"], timeout=10)
    # Give the adapter a moment to collect results
    time.sleep(2)

    result = _run_nmcli([
        "--terse",
        "--fields", "IN-USE,SSID,SIGNAL,SECURITY",
        "device", "wifi", "list",
    ])

    if result.returncode != 0:
        logger.error("WiFi scan failed: %s", result.stderr.strip())
        return []

    networks: dict[str, WiFiNetwork] = {}
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # Terse format uses ':' as separator. IN-USE is '*' or ' '.
        # Handle escaped colons in SSID by splitting carefully.
        parts = line.split(":")
        if len(parts) < 4:
            continue

        in_use = parts[0].strip() == "*"
        ssid = parts[1].strip()
        if not ssid:
            continue  # Skip hidden networks

        try:
            signal = int(parts[2].strip())
        except ValueError:
            signal = 0

        security = parts[3].strip() if len(parts) > 3 else "Open"
        if not security or security == "--":
            security = "Open"

        # Keep the entry with the strongest signal for each SSID
        if ssid not in networks or signal > networks[ssid].signal:
            networks[ssid] = WiFiNetwork(
                ssid=ssid,
                signal=signal,
                security=security,
                in_use=in_use,
            )

    result_list = sorted(networks.values(), key=lambda n: n.signal, reverse=True)
    return [asdict(n) for n in result_list]


def connect(ssid: str, password: str, timeout_seconds: int = 30) -> tuple[bool, str]:
    """
    Connect to a WiFi network.

    Args:
        ssid: The network SSID.
        password: The network password (empty string for open networks).
        timeout_seconds: How long to wait for the connection.

    Returns:
        A tuple of (success: bool, message: str).
    """
    if not ssid:
        return False, "SSID cannot be empty"

    logger.info("Attempting to connect to WiFi network: %s", ssid)

    # First, check if we already have a saved connection for this SSID
    existing = _run_nmcli(["--terse", "--fields", "NAME", "connection", "show"])
    saved_names = [line.strip() for line in existing.stdout.strip().split("\n") if line.strip()]

    if ssid in saved_names:
        # Delete old connection profile to ensure fresh credentials
        logger.info("Removing existing connection profile for %s", ssid)
        _run_nmcli(["connection", "delete", ssid])
        time.sleep(1)

    # Build the connect command
    cmd = ["device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]

    result = _run_nmcli(cmd, timeout=timeout_seconds)

    if result.returncode == 0:
        logger.info("Successfully connected to %s", ssid)
        # Wait a moment for IP assignment
        time.sleep(3)
        status = get_status()
        if status.connected:
            return True, f"Connected to {ssid} with IP {status.ip_address}"
        return True, f"Connected to {ssid}, waiting for IP assignment"

    error_msg = result.stderr.strip() or result.stdout.strip()
    logger.error("Failed to connect to %s: %s", ssid, error_msg)

    # Provide user-friendly error messages
    if "Secrets were required" in error_msg or "password" in error_msg.lower():
        return False, "Incorrect password"
    if "No network with SSID" in error_msg:
        return False, f"Network '{ssid}' not found"
    if "timeout" in error_msg.lower():
        return False, "Connection timed out"

    return False, f"Connection failed: {error_msg}"


def get_status() -> WiFiStatus:
    """Get the current WiFi connection status."""
    result = _run_nmcli([
        "--terse",
        "--fields", "DEVICE,TYPE,STATE,CONNECTION",
        "device", "status",
    ])

    if result.returncode != 0:
        return WiFiStatus(connected=False)

    wifi_connected = False
    connection_name = None

    for line in result.stdout.strip().split("\n"):
        parts = line.split(":")
        if len(parts) >= 4 and parts[1] == "wifi":
            if parts[2] == "connected":
                wifi_connected = True
                connection_name = parts[3]
            break

    if not wifi_connected:
        return WiFiStatus(connected=False)

    # Get detailed info about the active WiFi connection
    status = WiFiStatus(connected=True, ssid=connection_name)

    ip_result = _run_nmcli([
        "--terse",
        "--fields", "IP4.ADDRESS",
        "device", "show", "wlan0",
    ])
    if ip_result.returncode == 0:
        for line in ip_result.stdout.strip().split("\n"):
            if "IP4.ADDRESS" in line:
                # Format is IP4.ADDRESS[1]:192.168.1.100/24
                parts = line.split(":")
                if len(parts) >= 2:
                    ip_with_prefix = parts[1].strip()
                    status.ip_address = ip_with_prefix.split("/")[0]
                break

    return status


def disconnect() -> tuple[bool, str]:
    """Disconnect from the current WiFi network."""
    result = _run_nmcli(["device", "disconnect", "wlan0"])
    if result.returncode == 0:
        logger.info("Disconnected from WiFi")
        return True, "Disconnected"
    error_msg = result.stderr.strip() or result.stdout.strip()
    logger.error("Failed to disconnect: %s", error_msg)
    return False, f"Disconnect failed: {error_msg}"


def forget_network(ssid: str) -> tuple[bool, str]:
    """Remove a saved WiFi network profile."""
    if not ssid:
        return False, "SSID cannot be empty"

    result = _run_nmcli(["connection", "delete", ssid])
    if result.returncode == 0:
        logger.info("Forgot network: %s", ssid)
        return True, f"Removed saved network '{ssid}'"
    error_msg = result.stderr.strip() or result.stdout.strip()
    logger.error("Failed to forget network %s: %s", ssid, error_msg)
    return False, f"Failed to remove network: {error_msg}"


def get_saved_networks() -> list[str]:
    """List all saved WiFi network profiles."""
    result = _run_nmcli([
        "--terse",
        "--fields", "NAME,TYPE",
        "connection", "show",
    ])
    if result.returncode != 0:
        return []

    networks = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split(":")
        if len(parts) >= 2 and parts[1].strip() == "802-11-wireless":
            networks.append(parts[0].strip())
    return networks
