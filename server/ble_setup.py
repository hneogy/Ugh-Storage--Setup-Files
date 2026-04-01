#!/usr/bin/env python3
"""
BLE GATT server for WiFi provisioning and device registration on Raspberry Pi 5.

Advertises a BLE service named "UghStorage-Setup" that allows an iOS app
to scan WiFi networks, configure credentials, check connection status,
and register the device with Supabase over Bluetooth Low Energy.

Uses the bluez D-Bus API via dbus-next for GATT server functionality.
Requires: bluez, python3-dbus-next, NetworkManager.
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
from typing import Optional

try:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method, dbus_property, PropertyAccess
    from dbus_next import Variant, BusType
except ImportError:
    print("ERROR: dbus-next library not found. Install with: pip install dbus-next")
    sys.exit(1)

import wifi_manager
from registration import register_device, save_device_config, setup_cloudflared_tunnel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_WIFI_SCAN_UUID = "12345678-1234-5678-1234-56789abcdef1"
CHAR_WIFI_CONFIG_UUID = "12345678-1234-5678-1234-56789abcdef2"
CHAR_WIFI_STATUS_UUID = "12345678-1234-5678-1234-56789abcdef3"
CHAR_SERVER_URL_UUID = "12345678-1234-5678-1234-56789abcdef4"
CHAR_DEVICE_INFO_UUID = "12345678-1234-5678-1234-56789abcdef5"
CHAR_USER_TOKEN_UUID = "12345678-1234-5678-1234-56789ABCDEF6"
CHAR_REGISTRATION_STATUS_UUID = "12345678-1234-5678-1234-56789ABCDEF7"

ADAPTER_NAME = "hci0"
DEVICE_NAME = "UghStorage-Setup"
APP_PATH = "/com/ughstorage/ble"
SERVICE_PATH = f"{APP_PATH}/service0"
ADVERT_PATH = f"{APP_PATH}/advertisement0"

CLOUDFLARE_TUNNEL_URL_FILE = "/etc/ughstorage/tunnel_url"
VERSION_FILE = "/etc/ughstorage/version"
APP_VERSION = "2.0.0"

BLUEZ_SERVICE = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
GATT_MANAGER_INTERFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("ble_setup")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_file_or(path: str, default: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def _get_tunnel_url() -> str:
    return _read_file_or(CLOUDFLARE_TUNNEL_URL_FILE, "")


def _get_version() -> str:
    return _read_file_or(VERSION_FILE, APP_VERSION)


def _get_device_info() -> dict:
    hostname = platform.node()
    storage_total = "0"
    storage_used = "0"
    try:
        stat = shutil.disk_usage("/")
        storage_total = str(stat.total)
        storage_used = str(stat.used)
    except Exception:
        pass
    return {
        "hostname": hostname,
        "storage_total": storage_total,
        "storage_used": storage_used,
        "version": _get_version(),
    }


def _to_byte_array(s: str) -> list[int]:
    """Convert a string to a list of bytes (D-Bus ay type)."""
    return list(s.encode("utf-8"))


def _from_byte_array(data: list[int]) -> str:
    """Convert a list of bytes back to a string."""
    return bytes(data).decode("utf-8")


# ---------------------------------------------------------------------------
# D-Bus object: GATT Characteristic
# ---------------------------------------------------------------------------

GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"

# Total number of characteristics (0..6)
NUM_CHARACTERISTICS = 7


class BLEApplication:
    """
    Manages the full BLE GATT application lifecycle:
    advertising, GATT service registration, and characteristic handling.
    """

    def __init__(self):
        self._bus: Optional[MessageBus] = None
        self._wifi_status_notify_task: Optional[asyncio.Task] = None
        self._running = True
        # Cached characteristic values
        self._wifi_status_value: bytes = b""
        self._registration_status_value: str = ""
        self._server_url_override: Optional[str] = None
        self._notify_callbacks: list = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the BLE application and block until stopped."""
        logger.info("Starting BLE GATT server...")

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        logger.info("Connected to system D-Bus")

        await self._configure_adapter()
        await self._register_application()
        await self._register_advertisement()

        logger.info("BLE GATT server is running. Advertising as '%s'", DEVICE_NAME)
        logger.info("Service UUID: %s", SERVICE_UUID)

        # Periodically update WiFi status for notify subscribers
        self._wifi_status_notify_task = asyncio.create_task(self._wifi_status_loop())

        # Block until shutdown
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        self._running = False
        if self._wifi_status_notify_task:
            self._wifi_status_notify_task.cancel()

    # ------------------------------------------------------------------
    # Adapter configuration
    # ------------------------------------------------------------------

    async def _configure_adapter(self) -> None:
        """Enable the BT adapter and set it discoverable."""
        logger.info("Configuring Bluetooth adapter %s...", ADAPTER_NAME)

        # Use hciconfig/bluetoothctl as a reliable way to bring up the adapter
        subprocess.run(["sudo", "hciconfig", ADAPTER_NAME, "up"], check=False)
        subprocess.run(
            ["sudo", "hciconfig", ADAPTER_NAME, "leadv", "3"],
            check=False,
        )

        # Set adapter properties via D-Bus
        introspection = await self._bus.introspect(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}"
        )
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}", introspection
        )
        props = proxy.get_interface(DBUS_PROP_IFACE)

        try:
            await props.call_set(ADAPTER_INTERFACE, "Powered", Variant("b", True))
        except Exception as exc:
            logger.debug("Setting Powered: %s", exc)

        try:
            await props.call_set(ADAPTER_INTERFACE, "Alias", Variant("s", DEVICE_NAME))
        except Exception as exc:
            logger.debug("Setting Alias: %s", exc)

        try:
            await props.call_set(ADAPTER_INTERFACE, "Discoverable", Variant("b", True))
        except Exception as exc:
            logger.debug("Setting Discoverable: %s", exc)

        try:
            await props.call_set(
                ADAPTER_INTERFACE, "DiscoverableTimeout", Variant("u", 0)
            )
        except Exception as exc:
            logger.debug("Setting DiscoverableTimeout: %s", exc)

        logger.info("Adapter configured")

    # ------------------------------------------------------------------
    # GATT application registration (using raw D-Bus messages)
    # ------------------------------------------------------------------

    async def _register_application(self) -> None:
        """Register the GATT application tree with bluez."""
        # We export our objects on the bus before calling RegisterApplication.
        self._export_objects()

        introspection = await self._bus.introspect(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}"
        )
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}", introspection
        )
        gatt_manager = proxy.get_interface(GATT_MANAGER_INTERFACE)

        try:
            await gatt_manager.call_register_application(APP_PATH, {})
            logger.info("GATT application registered at %s", APP_PATH)
        except Exception as exc:
            logger.error("Failed to register GATT application: %s", exc)
            raise

    async def _register_advertisement(self) -> None:
        """Register LE advertisement with bluez."""
        introspection = await self._bus.introspect(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}"
        )
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}", introspection
        )
        ad_manager = proxy.get_interface(LE_ADVERTISING_MANAGER_INTERFACE)

        try:
            await ad_manager.call_register_advertisement(ADVERT_PATH, {})
            logger.info("LE advertisement registered at %s", ADVERT_PATH)
        except Exception as exc:
            logger.error("Failed to register advertisement: %s", exc)
            raise

    # ------------------------------------------------------------------
    # D-Bus object tree
    # ------------------------------------------------------------------

    def _export_objects(self) -> None:
        """Export the ObjectManager, GATT service, characteristics, and advertisement."""
        # ObjectManager at APP_PATH
        self._bus.export(APP_PATH, AppObjectManager(self))

        # GATT service
        self._bus.export(SERVICE_PATH, GattServiceObject())

        # Characteristics
        chars = [
            (0, CHAR_WIFI_SCAN_UUID, ["read"], self._handle_wifi_scan_read, None),
            (1, CHAR_WIFI_CONFIG_UUID, ["write"], None, self._handle_wifi_config_write),
            (2, CHAR_WIFI_STATUS_UUID, ["read", "notify"], self._handle_wifi_status_read, None),
            (3, CHAR_SERVER_URL_UUID, ["read"], self._handle_server_url_read, None),
            (4, CHAR_DEVICE_INFO_UUID, ["read"], self._handle_device_info_read, None),
            (5, CHAR_USER_TOKEN_UUID, ["write"], None, self._handle_user_token_write),
            (6, CHAR_REGISTRATION_STATUS_UUID, ["read", "notify"], self._handle_registration_status_read, None),
        ]

        for idx, char_uuid, flags, read_handler, write_handler in chars:
            path = f"{SERVICE_PATH}/char{idx}"
            char_obj = GattCharacteristicObject(
                uuid=char_uuid,
                flags=flags,
                service_path=SERVICE_PATH,
                read_handler=read_handler,
                write_handler=write_handler,
                start_notify_handler=self._handle_start_notify if idx in (2, 6) else None,
                stop_notify_handler=self._handle_stop_notify if idx in (2, 6) else None,
            )
            self._bus.export(path, char_obj)

        # Advertisement
        self._bus.export(ADVERT_PATH, LEAdvertisementObject())

        logger.info("D-Bus objects exported")

    # ------------------------------------------------------------------
    # Characteristic handlers
    # ------------------------------------------------------------------

    async def _handle_wifi_scan_read(self) -> list[int]:
        """Handle read on WIFI_SCAN characteristic."""
        logger.info("WIFI_SCAN: read requested")
        try:
            networks = await asyncio.get_event_loop().run_in_executor(
                None, wifi_manager.scan_networks
            )
            payload = json.dumps(networks, separators=(",", ":"))
            logger.info("WIFI_SCAN: returning %d networks (%d bytes)", len(networks), len(payload))
            return _to_byte_array(payload)
        except Exception as exc:
            logger.error("WIFI_SCAN read failed: %s", exc)
            return _to_byte_array(json.dumps({"error": str(exc)}))

    async def _handle_wifi_config_write(self, value: list[int]) -> None:
        """Handle write on WIFI_CONFIG characteristic."""
        raw = _from_byte_array(value)
        logger.info("WIFI_CONFIG: write received (%d bytes)", len(raw))
        try:
            config = json.loads(raw)
            ssid = config.get("ssid", "")
            password = config.get("password", "")
            if not ssid:
                logger.warning("WIFI_CONFIG: empty SSID")
                return

            logger.info("WIFI_CONFIG: connecting to '%s'...", ssid)
            success, message = await asyncio.get_event_loop().run_in_executor(
                None, wifi_manager.connect, ssid, password
            )
            logger.info("WIFI_CONFIG: result=%s message=%s", success, message)

            # Update cached status and trigger notify
            await self._update_wifi_status()

        except json.JSONDecodeError as exc:
            logger.error("WIFI_CONFIG: invalid JSON: %s", exc)
        except Exception as exc:
            logger.error("WIFI_CONFIG: error: %s", exc)

    async def _handle_wifi_status_read(self) -> list[int]:
        """Handle read on WIFI_STATUS characteristic."""
        logger.info("WIFI_STATUS: read requested")
        try:
            wifi_status = await asyncio.get_event_loop().run_in_executor(
                None, wifi_manager.get_status
            )
            payload = json.dumps(wifi_status.to_dict(), separators=(",", ":"))
            logger.info("WIFI_STATUS: %s", payload)
            return _to_byte_array(payload)
        except Exception as exc:
            logger.error("WIFI_STATUS read failed: %s", exc)
            return _to_byte_array(json.dumps({"connected": False, "error": str(exc)}))

    async def _handle_server_url_read(self) -> list[int]:
        """Handle read on SERVER_URL characteristic."""
        if self._server_url_override:
            url = self._server_url_override
        else:
            url = _get_tunnel_url()
        logger.info("SERVER_URL: returning '%s'", url)
        return _to_byte_array(url)

    async def _handle_device_info_read(self) -> list[int]:
        """Handle read on DEVICE_INFO characteristic."""
        info = _get_device_info()
        payload = json.dumps(info, separators=(",", ":"))
        logger.info("DEVICE_INFO: %s", payload)
        return _to_byte_array(payload)

    async def _handle_user_token_write(self, value: list[int]) -> None:
        """Handle write on USER_TOKEN characteristic.

        When the iOS app writes the user's Supabase JWT here, we:
        1. Call registration.register_device() with the token
        2. Update REGISTRATION_STATUS with progress
        3. Save config via registration.save_device_config()
        4. Update SERVER_URL with the tunnel_url
        """
        raw = _from_byte_array(value)
        logger.info("USER_TOKEN: write received (%d bytes)", len(raw))

        try:
            # Step 1: Update status to registering
            self._registration_status_value = json.dumps(
                {"status": "registering", "message": "Registering device with cloud..."},
                separators=(",", ":"),
            )
            logger.info("REGISTRATION_STATUS: registering")

            # Step 2: Call register_device
            result = await register_device(raw.strip())
            logger.info("USER_TOKEN: registration response: %s", result)

            # Step 3: Update status to provisioning_tunnel
            self._registration_status_value = json.dumps(
                {"status": "provisioning_tunnel", "message": "Setting up secure tunnel..."},
                separators=(",", ":"),
            )
            logger.info("REGISTRATION_STATUS: provisioning_tunnel")

            # Step 4: Provision Cloudflare Tunnel
            await setup_cloudflared_tunnel(result["tunnel_token"])

            # Step 5: Update status to saving_config
            self._registration_status_value = json.dumps(
                {"status": "saving_config", "message": "Saving device configuration..."},
                separators=(",", ":"),
            )
            logger.info("REGISTRATION_STATUS: saving_config")

            # Step 6: Save config
            save_device_config(
                device_id=result["device_id"],
                shared_secret=result["shared_secret"],
                subdomain=result["subdomain"],
                tunnel_url=result["tunnel_url"],
                tunnel_token=result["tunnel_token"],
            )

            # Step 7: Update SERVER_URL with tunnel_url
            self._server_url_override = result["tunnel_url"]

            # Step 8: Mark complete
            self._registration_status_value = json.dumps(
                {
                    "status": "complete",
                    "message": "Registration complete!",
                    "device_id": result["device_id"],
                    "tunnel_url": result["tunnel_url"],
                },
                separators=(",", ":"),
            )
            logger.info("REGISTRATION_STATUS: complete (device_id=%s)", result["device_id"])

        except Exception as exc:
            logger.error("USER_TOKEN: registration failed: %s", exc)
            self._registration_status_value = json.dumps(
                {"status": "error", "message": str(exc)},
                separators=(",", ":"),
            )

    async def _handle_registration_status_read(self) -> list[int]:
        """Handle read on REGISTRATION_STATUS characteristic."""
        logger.info("REGISTRATION_STATUS: read requested")
        if self._registration_status_value:
            return _to_byte_array(self._registration_status_value)
        return _to_byte_array(json.dumps(
            {"status": "idle", "message": "Waiting for user token"},
            separators=(",", ":"),
        ))

    # ------------------------------------------------------------------
    # Notify support for WIFI_STATUS and REGISTRATION_STATUS
    # ------------------------------------------------------------------

    async def _handle_start_notify(self) -> None:
        logger.info("Notifications started")

    async def _handle_stop_notify(self) -> None:
        logger.info("Notifications stopped")

    async def _update_wifi_status(self) -> None:
        """Refresh the cached WiFi status value."""
        try:
            wifi_status = await asyncio.get_event_loop().run_in_executor(
                None, wifi_manager.get_status
            )
            self._wifi_status_value = json.dumps(
                wifi_status.to_dict(), separators=(",", ":")
            ).encode("utf-8")
        except Exception as exc:
            logger.error("Failed to update WiFi status: %s", exc)

    async def _wifi_status_loop(self) -> None:
        """Periodically update WiFi status (used for notify)."""
        while self._running:
            try:
                await self._update_wifi_status()
            except Exception as exc:
                logger.debug("Status loop error: %s", exc)
            await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self) -> None:
        logger.info("Cleaning up BLE resources...")
        try:
            introspection = await self._bus.introspect(
                BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}"
            )
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, f"/org/bluez/{ADAPTER_NAME}", introspection
            )

            try:
                ad_manager = proxy.get_interface(LE_ADVERTISING_MANAGER_INTERFACE)
                await ad_manager.call_unregister_advertisement(ADVERT_PATH)
            except Exception:
                pass

            try:
                gatt_manager = proxy.get_interface(GATT_MANAGER_INTERFACE)
                await gatt_manager.call_unregister_application(APP_PATH)
            except Exception:
                pass
        except Exception:
            pass

        self._bus.disconnect()
        logger.info("Cleanup complete")


# ---------------------------------------------------------------------------
# D-Bus service objects
# ---------------------------------------------------------------------------


class AppObjectManager(ServiceInterface):
    """
    Implements org.freedesktop.DBus.ObjectManager for the GATT application.
    bluez calls GetManagedObjects to discover our service tree.
    """

    def __init__(self, app: BLEApplication):
        super().__init__(DBUS_OM_IFACE)
        self._app = app

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        """Return the object tree for the GATT application."""
        objects = {}

        # Service
        objects[SERVICE_PATH] = {
            GATT_SERVICE_IFACE: {
                "UUID": Variant("s", SERVICE_UUID),
                "Primary": Variant("b", True),
            }
        }

        # Characteristics
        char_defs = [
            (0, CHAR_WIFI_SCAN_UUID, ["read"]),
            (1, CHAR_WIFI_CONFIG_UUID, ["write"]),
            (2, CHAR_WIFI_STATUS_UUID, ["read", "notify"]),
            (3, CHAR_SERVER_URL_UUID, ["read"]),
            (4, CHAR_DEVICE_INFO_UUID, ["read"]),
            (5, CHAR_USER_TOKEN_UUID, ["write"]),
            (6, CHAR_REGISTRATION_STATUS_UUID, ["read", "notify"]),
        ]
        for idx, char_uuid, flags in char_defs:
            path = f"{SERVICE_PATH}/char{idx}"
            objects[path] = {
                GATT_CHRC_IFACE: {
                    "UUID": Variant("s", char_uuid),
                    "Service": Variant("o", SERVICE_PATH),
                    "Flags": Variant("as", flags),
                }
            }

        return objects


class GattServiceObject(ServiceInterface):
    """Represents a GATT Service on D-Bus."""

    def __init__(self):
        super().__init__(GATT_SERVICE_IFACE)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return SERVICE_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Characteristics(self) -> "ao":
        return [f"{SERVICE_PATH}/char{i}" for i in range(NUM_CHARACTERISTICS)]


class GattCharacteristicObject(ServiceInterface):
    """
    Represents a GATT Characteristic on D-Bus.
    Delegates read/write logic to provided handler callables.
    """

    def __init__(
        self,
        uuid: str,
        flags: list[str],
        service_path: str,
        read_handler=None,
        write_handler=None,
        start_notify_handler=None,
        stop_notify_handler=None,
    ):
        super().__init__(GATT_CHRC_IFACE)
        self._uuid = uuid
        self._flags = flags
        self._service_path = service_path
        self._read_handler = read_handler
        self._write_handler = write_handler
        self._start_notify_handler = start_notify_handler
        self._stop_notify_handler = stop_notify_handler
        self._notifying = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self._service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self._notifying

    @method()
    async def ReadValue(self, options: "a{sv}") -> "ay":
        """Handle a BLE read request."""
        if self._read_handler:
            try:
                return await self._read_handler()
            except Exception as exc:
                logger.error("ReadValue error on %s: %s", self._uuid, exc)
                return []
        return []

    @method()
    async def WriteValue(self, value: "ay", options: "a{sv}") -> None:
        """Handle a BLE write request."""
        if self._write_handler:
            try:
                await self._write_handler(value)
            except Exception as exc:
                logger.error("WriteValue error on %s: %s", self._uuid, exc)

    @method()
    async def StartNotify(self) -> None:
        if self._notifying:
            return
        self._notifying = True
        if self._start_notify_handler:
            await self._start_notify_handler()

    @method()
    async def StopNotify(self) -> None:
        if not self._notifying:
            return
        self._notifying = False
        if self._stop_notify_handler:
            await self._stop_notify_handler()


class LEAdvertisementObject(ServiceInterface):
    """
    Implements org.bluez.LEAdvertisement1 to advertise the GATT service.
    """

    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return DEVICE_NAME

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":
        return ["tx-power"]

    @method()
    def Release(self) -> None:
        logger.info("Advertisement released")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    app = BLEApplication()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.stop()))

    await app.run()


if __name__ == "__main__":
    logger.info("UghStorage BLE Setup Service starting")

    if not wifi_manager.check_nmcli_available():
        logger.warning(
            "NetworkManager (nmcli) is not available. "
            "WiFi operations will fail until it is installed."
        )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)
