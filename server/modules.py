"""Per-module registry and health aggregation.

The Pi runs one or more product modules — today only `storage` is live, and
Navidrome / Immich / Jellyfin land in later sprints. This module owns:

  - the canonical list of modules the Pi *could* run (mirrors the iOS
    `ModuleRegistry` so the contract is single-sourced)
  - per-module health probes (systemd-active + HTTP health endpoint)
  - the aggregator endpoint shape used by iOS to render a "Pi dashboard"

Design choices:

  - Registry is a plain Python dict, hardcoded here. Keeps it grep-able and
    avoids a yaml dep. Adding a future module is one entry.
  - Health is checked on demand (no cached background poll). At storage-only
    scale the cost is one HTTP roundtrip; we'll move to a cached/notified
    model if it ever matters.
  - Standard health envelope is `HealthEnvelope` below — every module's
    `/health` is expected to return this shape (or be wrapped to).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import aiohttp
from pydantic import BaseModel

from config import STORAGE_ROOT
from update_manager import current_version

logger = logging.getLogger(__name__)

# Canonical install methods. Mirror of the iOS InstallMethod enum string values.
InstallMethodLiteral = Literal["native", "docker", "docker_compose"]
ModuleId = Literal["storage", "music", "photos", "video"]
HealthStatus = Literal["ok", "degraded", "failed", "not_installed"]


class ModuleSpec(BaseModel):
    """Static metadata about one module. Mirrors iOS ModuleDescriptor — keep
    fields in sync when changing either side."""
    id: ModuleId
    display_name: str
    install_method: InstallMethodLiteral
    default_port: int
    health_path: str
    systemd_unit: str  # the unit name to query for active/inactive
    data_dir: str      # canonical filesystem location for the module's data


class ModuleStatus(BaseModel):
    """Runtime status of one module on this Pi. Facts only — the Pi does
    not know about user preferences (those live in Supabase and are
    reconciled by iOS against DeviceRecord.enabledModules)."""
    id: ModuleId
    installed: bool        # bits exist on disk (we proxy by data_dir presence)
    running: bool          # systemd reports the unit as active
    port: int              # actual listen port — for storage today, equals default_port
    health_url: str        # URL on the Pi the storage module uses to probe health


class HealthEnvelope(BaseModel):
    """Standardized health response. Every module's /health endpoint returns
    this shape (storage's own /health gets wrapped accordingly)."""
    module: ModuleId
    status: HealthStatus
    version: Optional[str] = None
    uptime_seconds: Optional[int] = None
    message: Optional[str] = None


class ModulesHealthReport(BaseModel):
    """Aggregator response — what iOS pulls to render the dashboard."""
    overall: HealthStatus
    modules: list[HealthEnvelope]


# --- Canonical paths for module data ---
# All under /mnt/nvme/ughstorage/ (NEW namespace). Pre-existing storage data
# under /mnt/nvme/storage/ is left alone — see migration note in setup.sh.
UGH_ROOT = Path("/mnt/nvme/ughstorage")
MEDIA_ROOT = UGH_ROOT / "media"
MODULE_DATA_ROOT = UGH_ROOT / "module-data"


# --- The registry ---
# Ports MUST not collide with anything else on the Pi. 8000 is storage today.
# Defaults below match each upstream's documented default; adjust when sprint
# (c+) lands the actual install scripts.
REGISTRY: dict[ModuleId, ModuleSpec] = {
    "storage": ModuleSpec(
        id="storage",
        display_name="Ugh! Storage",
        install_method="native",
        default_port=8000,
        health_path="/health",
        systemd_unit="ughstorage.service",
        data_dir=str(STORAGE_ROOT),  # legacy /mnt/nvme/storage, kept as-is
    ),
    "music": ModuleSpec(
        id="music",
        display_name="Navidrome",
        install_method="docker",
        default_port=4533,
        health_path="/ping",
        systemd_unit="ugh-module-music.service",
        # data_dir is the "module is installed" sentinel — the install
        # script's DATA_DIR, not the user's media library. Otherwise
        # ModuleStatus.installed would flip to True as soon as the user
        # creates an empty music folder.
        data_dir=str(MODULE_DATA_ROOT / "music"),
    ),
    "photos": ModuleSpec(
        id="photos",
        display_name="Immich",
        install_method="docker_compose",
        default_port=2283,
        health_path="/api/server-info/ping",
        systemd_unit="ugh-module-photos.service",
        data_dir=str(MODULE_DATA_ROOT / "immich"),
    ),
    "video": ModuleSpec(
        id="video",
        display_name="Jellyfin",
        install_method="docker",
        default_port=8096,
        # /System/Info/Public is unauth and stable across every Jellyfin
        # version we'd ship — safer than /health which came later.
        health_path="/System/Info/Public",
        systemd_unit="ugh-module-video.service",
        data_dir=str(MODULE_DATA_ROOT / "jellyfin"),
    ),
}


# Process-start time used to compute storage's uptime since the answer
# system uptime ≠ storage uptime. Set by main.py at startup.
STORAGE_START_TIME = time.time()


def _is_unit_active(unit: str) -> bool:
    """Return True if `systemctl is-active` reports the unit as active.
    Synchronous + fast (~5 ms); fine to call from async via to_thread."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def module_status(module_id: ModuleId) -> ModuleStatus:
    """Snapshot of one module's current state on this Pi. iOS combines this
    with the user's `DeviceRecord.enabledModules` to decide what to render."""
    spec = REGISTRY[module_id]
    data_dir_exists = Path(spec.data_dir).exists()
    return ModuleStatus(
        id=module_id,
        installed=data_dir_exists,
        running=_is_unit_active(spec.systemd_unit),
        port=spec.default_port,
        health_url=f"http://127.0.0.1:{spec.default_port}{spec.health_path}",
    )


def all_module_statuses() -> list[ModuleStatus]:
    """Catalog of every module the Pi knows about. Includes modules that
    aren't installed yet — iOS renders them as "Available, not installed."""
    return [module_status(mid) for mid in REGISTRY]


async def _probe_health(spec: ModuleSpec) -> HealthEnvelope:
    """Probe one module's HTTP health endpoint. Returns a HealthEnvelope
    regardless of outcome — never raises, so the aggregator can always
    return a complete report."""
    if not _is_unit_active(spec.systemd_unit):
        return HealthEnvelope(module=spec.id, status="not_installed",
                              message=f"{spec.systemd_unit} is not active")
    url = f"http://127.0.0.1:{spec.default_port}{spec.health_path}"
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status >= 500:
                    return HealthEnvelope(module=spec.id, status="failed",
                                          message=f"HTTP {resp.status} from {spec.health_path}")
                # Best-effort: try to parse our standard envelope. If the
                # module returns something else (typical for first-party
                # services like Navidrome's /ping), synthesize a basic envelope.
                try:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and "status" in data:
                        return HealthEnvelope(
                            module=spec.id,
                            status=data.get("status", "ok"),
                            version=data.get("version"),
                            uptime_seconds=data.get("uptime_seconds"),
                            message=data.get("message"),
                        )
                except Exception:
                    pass
                return HealthEnvelope(module=spec.id, status="ok")
    except asyncio.TimeoutError:
        return HealthEnvelope(module=spec.id, status="degraded",
                              message="Health probe timed out after 3s")
    except Exception as exc:
        return HealthEnvelope(module=spec.id, status="failed",
                              message=f"Health probe failed: {exc}")


async def health_report() -> ModulesHealthReport:
    """Aggregate health across every running module on the Pi. Probes run
    concurrently so total latency is bounded by the slowest single probe.
    Modules that aren't installed/running are simply omitted — iOS will
    show "not installed" by combining this with the catalog from /modules."""
    envelopes: list[HealthEnvelope] = []
    other_targets: list[ModuleSpec] = []

    for mid, spec in REGISTRY.items():
        if mid == "storage":
            # Storage is in-process — we know its version + uptime without HTTP.
            envelopes.append(HealthEnvelope(
                module="storage",
                status="ok",
                version=current_version(),
                uptime_seconds=int(time.time() - STORAGE_START_TIME),
            ))
        elif _is_unit_active(spec.systemd_unit):
            other_targets.append(spec)
        # else: not running → omit from the report.

    if other_targets:
        results = await asyncio.gather(*[_probe_health(s) for s in other_targets])
        envelopes.extend(results)

    overall: HealthStatus = "ok"
    for env in envelopes:
        if env.status == "failed":
            overall = "failed"
            break
        if env.status in ("degraded", "not_installed"):
            overall = "degraded"
    return ModulesHealthReport(overall=overall, modules=envelopes)


# --- Install / uninstall state tracking ---
#
# Shell scripts under server/ (install_*.sh, uninstall_*.sh) do the heavy work
# — Docker pulls, systemd unit writes, health verification. They write progress
# into a shared JSON file here so the iOS app can poll.

SERVER_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("UGHSTORAGE_STATE_DIR", "/var/lib/ughstorage"))
MODULE_STATE_FILE = STATE_DIR / "modules-state.json"

# Lifecycle states. Mirrors iOS ModuleLifecycleState so decoding is trivial.
ModuleLifecycleState = Literal[
    "idle",          # nothing in flight; module either not installed or steady-state running
    "installing",    # install script is actively running
    "uninstalling",  # uninstall script is actively running
    "failed",        # terminal failure from install or uninstall
]


class ModuleLifecycleStatus(BaseModel):
    module: ModuleId
    state: ModuleLifecycleState
    updated_at: Optional[str] = None
    step: Optional[str] = None        # human-readable step, e.g. "Pulling image"
    message: Optional[str] = None


def _read_lifecycle() -> dict[str, dict[str, Any]]:
    try:
        with MODULE_STATE_FILE.open("r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def lifecycle_for(module_id: ModuleId) -> ModuleLifecycleStatus:
    """Return the lifecycle status for one module. `idle` if nothing on disk."""
    data = _read_lifecycle().get(module_id, {})
    return ModuleLifecycleStatus(
        module=module_id,
        state=data.get("state", "idle"),
        updated_at=data.get("updated_at"),
        step=data.get("step"),
        message=data.get("message"),
    )


def _write_lifecycle(module_id: ModuleId, **fields: Any) -> None:
    """Merge-update this module's entry in the shared state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_lifecycle()
    entry = data.get(module_id, {})
    entry.update(fields)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    data[module_id] = entry
    MODULE_STATE_FILE.write_text(json.dumps(data))


async def spawn_lifecycle_script(module_id: ModuleId, action: Literal["install", "uninstall"]) -> ModuleLifecycleStatus:
    """Run `<action>_<module_id>.sh` as a detached process so the lifecycle
    survives the main uvicorn restart. Refuses to spawn if an action is
    already in flight for this module."""
    existing = lifecycle_for(module_id)
    if existing.state in ("installing", "uninstalling"):
        return existing  # already in flight — iOS polls status

    script_name = f"{action}_{module_id}.sh"
    script_path = SERVER_DIR / script_name
    if not script_path.exists():
        _write_lifecycle(module_id, state="failed",
                         step=None, message=f"{script_name} not found")
        return lifecycle_for(module_id)

    # Write initial state before forking so iOS sees progress immediately.
    initial_state: ModuleLifecycleState = "installing" if action == "install" else "uninstalling"
    _write_lifecycle(module_id, state=initial_state,
                     step="Starting", message=None)

    log_file = STATE_DIR / f"module-{module_id}-{action}.log"
    await asyncio.create_subprocess_exec(
        "/bin/bash", str(script_path),
        cwd=str(SERVER_DIR),
        stdout=log_file.open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return lifecycle_for(module_id)


# --- Storage usage breakdown ---
# Computing disk usage for a top-level dir scans every file underneath; that's
# cheap for hundreds of MB, painful for TB-scale libraries. Cache the result
# for 60s so iOS can poll the dashboard without hammering the drive.

_usage_cache: tuple[float, dict[str, int]] | None = None
_USAGE_CACHE_TTL_SECS = 60.0


def _dir_size_bytes(path: Path) -> int:
    """Recursively sum regular-file sizes under `path`. Silently skips
    anything we can't stat (permission errors, symlink loops, etc.)."""
    if not path.exists():
        return 0
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
            except (OSError, FileNotFoundError):
                continue
    except OSError:
        pass
    return total


def usage_breakdown() -> dict[str, int]:
    """Per-module bytes + system + free + total under the storage root.
    Cached for 60s because scanning large libraries isn't cheap.

    Keys: storage, music, photos, video, thumbnails, hls, system, free, total.
    `system` = everything under the mount that isn't accounted for above
              (includes ugh/module-data, DB files, etc.).
    """
    global _usage_cache
    now = time.time()
    if _usage_cache and now - _usage_cache[0] < _USAGE_CACHE_TTL_SECS:
        return _usage_cache[1]

    import shutil
    total, _used, free = shutil.disk_usage(str(STORAGE_ROOT))

    storage_bytes = _dir_size_bytes(STORAGE_ROOT)
    music_bytes = _dir_size_bytes(MEDIA_ROOT / "music")
    photos_bytes = _dir_size_bytes(MEDIA_ROOT / "photos")
    video_bytes = _dir_size_bytes(MEDIA_ROOT / "video")
    # thumbnails + hls are under /mnt/nvme/ but outside the ugh root.
    # Point at them via config.
    from config import THUMBNAIL_ROOT
    try:
        from config import HLS_ROOT
    except ImportError:
        HLS_ROOT = Path("/mnt/nvme/hls")
    thumbnails_bytes = _dir_size_bytes(THUMBNAIL_ROOT)
    hls_bytes = _dir_size_bytes(HLS_ROOT)

    known = storage_bytes + music_bytes + photos_bytes + video_bytes + thumbnails_bytes + hls_bytes
    used = total - free
    system_bytes = max(0, used - known)

    result = {
        "storage": storage_bytes,
        "music": music_bytes,
        "photos": photos_bytes,
        "video": video_bytes,
        "thumbnails": thumbnails_bytes,
        "hls": hls_bytes,
        "system": system_bytes,
        "free": free,
        "total": total,
    }
    _usage_cache = (now, result)
    return result


def is_disk_full(min_free_bytes: int = 1 * 1024 * 1024 * 1024) -> bool:
    """Return True when the drive is close enough to full that we should
    refuse new uploads. Default threshold is 1 GB, which leaves headroom for
    SQLite WAL, Immich's Postgres writes, thumbnail generation, etc.
    Fresh disk_usage read — no cache, because this gates a destructive write."""
    import shutil
    try:
        free = shutil.disk_usage(str(STORAGE_ROOT)).free
    except OSError:
        return False  # fail open rather than refuse writes on a bogus stat error
    return free < min_free_bytes


def read_module_env(module_id: ModuleId) -> dict[str, str]:
    """Read a module's .env file (written by its install script). Used by
    the credentials endpoint to return the admin user/password to iOS."""
    env_path = MODULE_DATA_ROOT / module_id / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


async def single_module_health(module_id: ModuleId) -> HealthEnvelope:
    """Health for one specific module — used by iOS when drilling into
    a module's settings, and by the standardized envelope returned from
    /modules/{id}/health."""
    if module_id not in REGISTRY:
        return HealthEnvelope(module=module_id, status="not_installed",
                              message=f"Unknown module: {module_id}")
    spec = REGISTRY[module_id]
    if module_id == "storage":
        return HealthEnvelope(
            module="storage",
            status="ok",
            version=current_version(),
            uptime_seconds=int(time.time() - STORAGE_START_TIME),
        )
    return await _probe_health(spec)
