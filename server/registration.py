"""Device registration and heartbeat for UghStorage multi-tenant support.

Called by the BLE setup flow to register this Pi with Supabase, and
periodically by the heartbeat endpoint to report online status.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

import aiohttp

import config

logger = logging.getLogger("registration")

# Path to the .env file that stores device credentials
_ENV_FILE = Path(__file__).resolve().parent / ".env"


async def register_device(user_token: str) -> dict:
    """Register this device with Supabase via the register-device edge function.

    Args:
        user_token: The user's Supabase JWT (from the iOS app, passed over BLE).

    Returns:
        dict with keys: device_id, subdomain, tunnel_url, shared_secret
    """
    url = f"{config.SUPABASE_URL}/functions/v1/register-device"
    headers = {
        "Authorization": f"Bearer {user_token}",
        "apikey": config.SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"register-device failed (HTTP {resp.status}): {body}"
                )
            data = await resp.json()

    # Expected shape: {device_id, subdomain, tunnel_url, shared_secret, tunnel_token}
    required_keys = ("device_id", "subdomain", "tunnel_url", "shared_secret", "tunnel_token")
    for key in required_keys:
        if key not in data:
            raise RuntimeError(f"register-device response missing '{key}': {data}")

    return data


_CLOUDFLARED_BIN = "/usr/local/bin/cloudflared"

_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Cloudflare Tunnel (UghStorage)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={bin} tunnel run --token {token}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


async def _run_cmd(*args: str, check: bool = True) -> tuple[int, str, str]:
    """Run a subprocess command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command {args} failed (rc={proc.returncode}): {stderr or stdout}"
        )
    return proc.returncode, stdout, stderr


async def setup_cloudflared_tunnel(tunnel_token: str) -> None:
    """Install and start a Cloudflare Tunnel using the provided token.

    Steps:
        1. Verify cloudflared binary exists.
        2. Stop any existing cloudflared service (ignore errors).
        3. Install via ``cloudflared service install <token>``.
        4. If that fails, write a systemd unit file manually.
        5. Start and verify the service.
    """
    # 1. Check binary
    if not os.path.isfile(_CLOUDFLARED_BIN):
        raise RuntimeError(
            f"cloudflared binary not found at {_CLOUDFLARED_BIN}. "
            "Run setup.sh first to install it."
        )
    logger.info("cloudflared binary found at %s", _CLOUDFLARED_BIN)

    # 2. Stop any existing service (ignore errors)
    logger.info("Stopping any existing cloudflared service...")
    await _run_cmd("sudo", "systemctl", "stop", "cloudflared", check=False)

    # 3. Try the official service install command
    logger.info("Installing cloudflared service with provided token...")
    rc, stdout, stderr = await _run_cmd(
        "sudo", _CLOUDFLARED_BIN, "service", "install", tunnel_token,
        check=False,
    )

    if rc != 0:
        logger.warning(
            "cloudflared service install failed (rc=%d): %s. "
            "Falling back to manual systemd unit.",
            rc, stderr or stdout,
        )

        # 4. Fallback: write a systemd unit manually
        unit_content = _SYSTEMD_UNIT_TEMPLATE.format(
            bin=_CLOUDFLARED_BIN, token=tunnel_token,
        )
        unit_path = "/etc/systemd/system/cloudflared.service"
        logger.info("Writing systemd unit to %s", unit_path)

        # Write via a temp file + sudo mv to handle permissions
        tmp_path = "/tmp/cloudflared.service"
        with open(tmp_path, "w") as f:
            f.write(unit_content)

        await _run_cmd("sudo", "cp", tmp_path, unit_path)
        await _run_cmd("sudo", "systemctl", "daemon-reload")
        await _run_cmd("sudo", "systemctl", "enable", "cloudflared")
    else:
        logger.info("cloudflared service install succeeded")

    # 5. Start the service
    logger.info("Starting cloudflared service...")
    await _run_cmd("sudo", "systemctl", "start", "cloudflared")

    # 6. Verify it's running
    rc, stdout, stderr = await _run_cmd(
        "sudo", "systemctl", "is-active", "cloudflared", check=False,
    )
    if rc == 0 and stdout == "active":
        logger.info("cloudflared service is running")
    else:
        logger.warning(
            "cloudflared service may not be running (is-active returned: %s)",
            stdout,
        )


def save_device_config(
    device_id: str,
    shared_secret: str,
    subdomain: str,
    tunnel_url: str,
    tunnel_token: str = "",
) -> None:
    """Write device credentials to the .env file and reload config module.

    This updates the existing .env file, adding or replacing the device-specific
    variables while preserving any other settings already present.
    """
    env_vars_to_set = {
        "UGHSTORAGE_DEVICE_ID": device_id,
        "UGHSTORAGE_DEVICE_SHARED_SECRET": shared_secret,
        "UGHSTORAGE_TUNNEL_TOKEN": tunnel_token,
    }

    # Read existing .env content (if any)
    existing_lines: list[str] = []
    if _ENV_FILE.exists():
        existing_lines = _ENV_FILE.read_text().splitlines()

    # Build a new .env, replacing lines for our keys or appending them
    keys_written: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        # Check if this line sets one of our target keys
        matched = False
        for key, value in env_vars_to_set.items():
            if stripped.startswith(f"{key}=") or stripped == key:
                new_lines.append(f"{key}={value}")
                keys_written.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    # Append any keys that were not already in the file
    for key, value in env_vars_to_set.items():
        if key not in keys_written:
            new_lines.append(f"{key}={value}")

    _ENV_FILE.write_text("\n".join(new_lines) + "\n")
    logger.info("Device config saved to %s", _ENV_FILE)

    # Reload the config module so in-process code sees the new values
    config.DEVICE_ID = device_id
    config.DEVICE_SHARED_SECRET = shared_secret

    # Also store the tunnel URL for the BLE characteristic
    tunnel_url_file = Path("/etc/ughstorage/tunnel_url")
    try:
        tunnel_url_file.parent.mkdir(parents=True, exist_ok=True)
        tunnel_url_file.write_text(tunnel_url)
        logger.info("Tunnel URL written to %s", tunnel_url_file)
    except PermissionError:
        logger.warning(
            "Could not write tunnel URL to %s (permission denied). "
            "You may need to run with sudo or write it manually.",
            tunnel_url_file,
        )


async def factory_reset() -> None:
    """Perform a factory reset: delete device from Supabase, stop tunnel, clear credentials, restart BLE.

    Each step is wrapped in a try/except so that a failure in one step does not
    prevent the remaining steps from executing.
    """
    # 1. Delete device record from Supabase
    try:
        if config.DEVICE_ID:
            url = f"{config.SUPABASE_URL}/rest/v1/devices"
            headers = {
                "apikey": config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
                "Content-Type": "application/json",
            }
            params = {"id": f"eq.{config.DEVICE_ID}"}
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=headers, params=params) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning("Failed to delete device from Supabase (HTTP %d): %s", resp.status, body)
                    else:
                        logger.info("Device record deleted from Supabase")
        else:
            logger.info("No DEVICE_ID set, skipping Supabase deletion")
    except Exception:
        logger.exception("Error deleting device from Supabase")

    # 2. Stop cloudflared service
    try:
        await _run_cmd("sudo", "systemctl", "stop", "cloudflared", check=False)
        logger.info("Stopped cloudflared service")
    except Exception:
        logger.exception("Error stopping cloudflared service")

    # 3. Clear DEVICE_ID and DEVICE_SHARED_SECRET from .env
    try:
        keys_to_remove = {"UGHSTORAGE_DEVICE_ID", "UGHSTORAGE_DEVICE_SHARED_SECRET", "UGHSTORAGE_TUNNEL_TOKEN"}
        if _ENV_FILE.exists():
            existing_lines = _ENV_FILE.read_text().splitlines()
            new_lines = [
                line for line in existing_lines
                if not any(line.strip().startswith(f"{key}=") or line.strip() == key for key in keys_to_remove)
            ]
            _ENV_FILE.write_text("\n".join(new_lines) + "\n")
            logger.info("Cleared device credentials from %s", _ENV_FILE)

        # Clear in-process config values
        config.DEVICE_ID = ""
        config.DEVICE_SHARED_SECRET = ""
    except Exception:
        logger.exception("Error clearing .env credentials")

    # 4. Restart ughstorage-ble service so Pi is discoverable again
    try:
        await _run_cmd("sudo", "systemctl", "restart", "ughstorage-ble", check=False)
        logger.info("Restarted ughstorage-ble service")
    except Exception:
        logger.exception("Error restarting ughstorage-ble service")


async def send_heartbeat() -> dict:
    """Send a heartbeat to Supabase with the Pi's online status and storage stats.

    Returns:
        The JSON response from Supabase.
    """
    if not config.DEVICE_ID or not config.DEVICE_SHARED_SECRET:
        raise RuntimeError("Device not registered")

    # Gather storage stats
    try:
        usage = shutil.disk_usage(str(config.STORAGE_ROOT))
        storage_stats = {
            "storage_total": usage.total,
            "storage_used": usage.used,
            "storage_free": usage.free,
        }
    except Exception:
        storage_stats = {
            "storage_total": 0,
            "storage_used": 0,
            "storage_free": 0,
        }

    url = f"{config.SUPABASE_URL}/rest/v1/devices"
    headers = {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    params = {"id": f"eq.{config.DEVICE_ID}"}
    payload = {
        "last_seen_at": "now()",
        "storage_total": storage_stats["storage_total"],
        "storage_used": storage_stats["storage_used"],
        "storage_free": storage_stats["storage_free"],
    }

    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=headers, params=params, json=payload) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                raise RuntimeError(
                    f"Heartbeat failed (HTTP {resp.status}): {body}"
                )
            if resp.status == 200:
                return await resp.json()
            return {"status": "ok"}
