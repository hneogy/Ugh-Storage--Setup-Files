"""UghStorage -- personal cloud storage server (multi-tenant)."""

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import platform
import secrets
import shutil
import socket
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from auth import require_auth
from config import (
    CORS_ORIGINS,
    DEVICE_ID,
    DEVICE_SHARED_SECRET,
    HLS_ROOT,
    HOST,
    MAX_UPLOAD_SIZE,
    PORT,
    STORAGE_ROOT,
    THUMBNAIL_ROOT,
)
from database import close_db, get_db, reset_db
from hls import hls_dir, hls_master_path, purge_hls, transcode_video_to_hls
import modules as ugh_modules
from registration import factory_reset, send_heartbeat
from thumbnails import generate_thumbnail
from update_manager import (
    check_for_update,
    current_git_sha,
    current_version,
    read_state,
    trigger_update,
)

logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _hls_backfill_scan() -> None:
    """Find any videos with no HLS output (never transcoded, or upgraded from
    a pre-HLS server) and queue one transcode at a time so we don't peg the
    Pi CPU on first boot after an update."""
    try:
        db = await get_db()
        cursor = await db.execute(
            """
            SELECT id, filename, path FROM files
            WHERE mime_type LIKE 'video/%' AND is_trashed = 0
              AND (hls_status IS NULL OR hls_status = 'pending')
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        rows = await cursor.fetchall()
        if not rows:
            return
        logger.info("HLS backfill: queueing %d videos", len(rows))
        for row in rows:
            src_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
            src = src_dir / f"{row['id']}_{row['filename']}"
            if src.exists():
                # Serialize: only one ffmpeg at a time during backfill so we
                # don't melt the Pi. Foreground uploads still preempt via the
                # background-tasks hook, but this loop won't try to run them in parallel.
                await transcode_video_to_hls(row["id"], src)
    except Exception:
        logger.exception("HLS backfill failed")


async def _hls_token_sweep_loop() -> None:
    """Delete expired HLS tokens once an hour. The table otherwise grows forever."""
    while True:
        try:
            await asyncio.sleep(3600)
            db = await get_db()
            now = datetime.now(timezone.utc).isoformat()
            await db.execute("DELETE FROM hls_tokens WHERE expires_at < ?", (now,))
            await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HLS token sweep failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: ensure directories exist
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    THUMBNAIL_ROOT.mkdir(parents=True, exist_ok=True)
    HLS_ROOT.mkdir(parents=True, exist_ok=True)
    # Ensure DB is initialized
    await get_db()
    # Kick off HLS bookkeeping out-of-band so the server is ready to serve
    # requests immediately; these run in the background while uvicorn accepts
    # connections.
    backfill_task = asyncio.create_task(_hls_backfill_scan())
    sweep_task = asyncio.create_task(_hls_token_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        backfill_task.cancel()
        await close_db()


app = FastAPI(title="UghStorage", version="2.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class MkdirRequest(BaseModel):
    path: str


class FileInfo(BaseModel):
    id: str
    filename: str
    path: str
    size: int
    mime_type: str
    created_at: str
    checksum: str
    thumbnail_url: str | None = None
    is_favorite: bool = False
    is_trashed: bool = False
    trashed_at: str | None = None
    # One of: None / "pending" / "transcoding" / "ready" / "failed" / "unsupported".
    # Only meaningful for video MIME types; non-videos leave it null.
    hls_status: str | None = None


class StorageStats(BaseModel):
    total: int
    used: int
    free: int


class RenameRequest(BaseModel):
    filename: str


class MoveRequest(BaseModel):
    destination: str


class BatchDeleteRequest(BaseModel):
    ids: list[str]


class ShareLinkRequest(BaseModel):
    expires_in: int = 86400  # seconds, default 24 hours


class ShareLinkResponse(BaseModel):
    url: str
    token: str
    expires_at: str


class WiFiConnectRequest(BaseModel):
    ssid: str
    password: str = ""

class DeviceRenameRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_path(path: str) -> str:
    """Normalize and validate a sub-path to prevent directory traversal."""
    cleaned = Path(path).as_posix().strip("/")
    if ".." in cleaned.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    return cleaned


async def _file_row_to_info(row) -> FileInfo:
    thumb_path = THUMBNAIL_ROOT / f"{row['id']}.jpg"
    thumb_url = f"/files/thumbnail/{row['id']}" if thumb_path.exists() else None
    hls_status = row["hls_status"] if "hls_status" in row.keys() else None
    return FileInfo(
        id=row["id"],
        filename=row["filename"],
        path=row["path"],
        size=row["size"],
        mime_type=row["mime_type"],
        created_at=row["created_at"],
        checksum=row["checksum"],
        thumbnail_url=thumb_url,
        is_favorite=bool(row["is_favorite"]) if "is_favorite" in row.keys() else False,
        is_trashed=bool(row["is_trashed"]) if "is_trashed" in row.keys() else False,
        trashed_at=row["trashed_at"] if "trashed_at" in row.keys() else None,
        hls_status=hls_status,
    )


async def _get_file_or_404(file_id: str):
    """Fetch a file row by ID or raise 404."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return row


# ---------------------------------------------------------------------------
# Health endpoint (unauthenticated)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Unauthenticated liveness probe. Used by update.sh's post-restart
    health check and by any external monitoring. Returns the standard
    HealthEnvelope shape with a couple of extra device-specific fields
    so existing monitoring keeps working."""
    return {
        "module": "storage",
        "status": "ok",
        "version": current_version(),
        "uptime_seconds": int(time.time() - ugh_modules.STORAGE_START_TIME),
        "device_id": DEVICE_ID,
    }


# ---------------------------------------------------------------------------
# Module registry — what runs on this Pi, where, and is it healthy?
# Sprint (b): storage is the only live module; the catalog still lists music /
# photos / video as "not installed" so iOS can render the future UI shape.
# ---------------------------------------------------------------------------

@app.get("/modules", response_model=list[ugh_modules.ModuleStatus])
async def list_modules(_user: str = Depends(require_auth)):
    """Catalog of every module the Pi knows about plus its current
    installed/running state. Reports facts only — user preferences live in
    Supabase (DeviceRecord.enabledModules) and are reconciled by iOS."""
    return ugh_modules.all_module_statuses()


@app.get("/modules/health", response_model=ugh_modules.ModulesHealthReport)
async def modules_health(_user: str = Depends(require_auth)):
    """Aggregated health across every running module. iOS polls this for the
    device dashboard. `overall` rolls up the per-module statuses."""
    return await ugh_modules.health_report()


@app.get("/modules/{module_id}/health", response_model=ugh_modules.HealthEnvelope)
async def module_health(module_id: str, _user: str = Depends(require_auth)):
    """Health for one specific module. Useful when the user has drilled
    into a module's settings and wants the freshest read without polling
    the whole aggregator."""
    if module_id not in ugh_modules.REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module_id}")
    return await ugh_modules.single_module_health(module_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Module lifecycle — install / uninstall / per-module settings.
# Sprint (c) lands the music module; photos and video follow the same shape.
# ---------------------------------------------------------------------------

@app.get("/modules/{module_id}/lifecycle", response_model=ugh_modules.ModuleLifecycleStatus)
async def module_lifecycle_status(module_id: str, _user: str = Depends(require_auth)):
    """Return the current install/uninstall progress for one module.
    iOS polls this during an in-flight install."""
    if module_id not in ugh_modules.REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module_id}")
    return ugh_modules.lifecycle_for(module_id)  # type: ignore[arg-type]


@app.post("/modules/{module_id}/install", response_model=ugh_modules.ModuleLifecycleStatus)
async def module_install(module_id: str, _user: str = Depends(require_auth)):
    """Trigger `install_<module>.sh` detached. Returns immediately with the
    initial "installing" state; iOS polls lifecycle_for to track progress."""
    if module_id not in ugh_modules.REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module_id}")
    if module_id == "storage":
        raise HTTPException(status_code=400, detail="Storage is always installed")
    return await ugh_modules.spawn_lifecycle_script(module_id, "install")  # type: ignore[arg-type]


@app.post("/modules/{module_id}/uninstall", response_model=ugh_modules.ModuleLifecycleStatus)
async def module_uninstall(module_id: str, _user: str = Depends(require_auth)):
    """Trigger `uninstall_<module>.sh` detached. User's data is preserved —
    see each module's uninstall script for what's kept vs removed."""
    if module_id not in ugh_modules.REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module_id}")
    if module_id == "storage":
        raise HTTPException(status_code=400, detail="Storage cannot be uninstalled")
    return await ugh_modules.spawn_lifecycle_script(module_id, "uninstall")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Music module — Navidrome-specific endpoints.
# ---------------------------------------------------------------------------

@app.get("/modules/music/credentials")
async def music_credentials(_user: str = Depends(require_auth)):
    """Return admin user + password written by install_music.sh. iOS renders
    these for the user to type into a Subsonic client, or to deep-link with.
    Returns 404 if the module hasn't been installed yet."""
    env = ugh_modules.read_module_env("music")
    user = env.get("NAVIDROME_ADMIN_USER")
    password = env.get("NAVIDROME_ADMIN_PASSWORD")
    if not user or not password:
        raise HTTPException(status_code=404, detail="Music module not installed")
    spec = ugh_modules.REGISTRY["music"]
    return {
        "username": user,
        "password": password,
        "port": spec.default_port,
        "version": env.get("NAVIDROME_VERSION"),
    }


@app.post("/modules/music/scan")
async def music_scan(_user: str = Depends(require_auth)):
    """Trigger a Navidrome library scan. Forwards to the Subsonic-flavored
    `getScanStatus` + `startScan` endpoints Navidrome exposes at
    /rest/startScan. We authenticate with the admin creds the install script
    wrote — iOS doesn't need to carry them."""
    env = ugh_modules.read_module_env("music")
    user = env.get("NAVIDROME_ADMIN_USER")
    password = env.get("NAVIDROME_ADMIN_PASSWORD")
    if not user or not password:
        raise HTTPException(status_code=404, detail="Music module not installed")
    spec = ugh_modules.REGISTRY["music"]
    # Subsonic API expects username, plain or token+salt, client, version, format.
    # Navidrome accepts plain password when the query is over loopback.
    params = {
        "u": user,
        "p": password,
        "c": "ughstorage",
        "v": "1.16.1",
        "f": "json",
    }
    url = f"http://127.0.0.1:{spec.default_port}/rest/startScan"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(url, params=params) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail=f"Navidrome returned {resp.status}")
                return {"status": "scan_started"}
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=502, detail=f"Navidrome unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# Photos module — Immich-specific endpoints.
# ---------------------------------------------------------------------------

class MLToggleRequest(BaseModel):
    enabled: bool


@app.get("/modules/photos/credentials")
async def photos_credentials(_user: str = Depends(require_auth)):
    """Admin email + password for Immich. iOS uses these to auth + to
    pre-fill the login form in the Immich app (when deep-linking supports it)."""
    env = ugh_modules.read_module_env("photos")
    # install_photos.sh writes to ughstorage.env, not the compose .env — the
    # latter contains DB creds that shouldn't escape the Pi.
    # read_module_env defaults to .env; redirect to the correct file here.
    ugh_env_path = ugh_modules.MODULE_DATA_ROOT / "immich" / "ughstorage.env"
    if not ugh_env_path.exists():
        raise HTTPException(status_code=404, detail="Photos module not installed")
    env = {}
    for line in ugh_env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    email = env.get("ADMIN_EMAIL")
    password = env.get("ADMIN_PASSWORD")
    if not email or not password:
        raise HTTPException(status_code=404, detail="Admin credentials not yet provisioned")
    spec = ugh_modules.REGISTRY["photos"]
    return {
        "email": email,
        "password": password,
        "name": env.get("ADMIN_NAME"),
        "port": spec.default_port,
        "version": env.get("IMMICH_VERSION"),
    }


@app.get("/modules/photos/ml-enabled")
async def photos_ml_enabled(_user: str = Depends(require_auth)):
    """Return whether the Immich ML container is currently running.
    Source of truth is the `.env` flag the iOS toggle writes — the actual
    container state follows within a few seconds of the toggle."""
    env_path = ugh_modules.MODULE_DATA_ROOT / "immich" / ".env"
    if not env_path.exists():
        raise HTTPException(status_code=404, detail="Photos module not installed")
    value = "true"
    for line in env_path.read_text().splitlines():
        if line.startswith("IMMICH_ENABLE_ML="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    return {"enabled": value.lower() == "true"}


@app.post("/modules/photos/ml-enabled")
async def photos_ml_toggle(body: MLToggleRequest, _user: str = Depends(require_auth)):
    """Flip Immich's ML container on or off. Updates the .env flag and
    restarts the systemd unit; the unit's ExecStart decides whether to
    include the `ml` compose profile based on the flag."""
    env_path = ugh_modules.MODULE_DATA_ROOT / "immich" / ".env"
    if not env_path.exists():
        raise HTTPException(status_code=404, detail="Photos module not installed")

    new_value = "true" if body.enabled else "false"
    lines = env_path.read_text().splitlines()
    wrote = False
    for i, line in enumerate(lines):
        if line.startswith("IMMICH_ENABLE_ML="):
            lines[i] = f"IMMICH_ENABLE_ML={new_value}"
            wrote = True
            break
    if not wrote:
        lines.append(f"IMMICH_ENABLE_ML={new_value}")
    env_path.write_text("\n".join(lines) + "\n")

    # Cycle the unit so the new ExecStart picks up the new profile. Runs in
    # the background so the HTTP response isn't blocked on Docker shuffling.
    async def _cycle() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "/bin/systemctl", "restart", "ugh-module-photos.service"
            )
            await proc.communicate()
        except Exception:
            logger.exception("Failed to restart photos unit after ML toggle")

    asyncio.create_task(_cycle())
    return {"enabled": body.enabled}


# ---------------------------------------------------------------------------
# Video module — Jellyfin-specific endpoints.
# ---------------------------------------------------------------------------

@app.get("/modules/video/credentials")
async def video_credentials(_user: str = Depends(require_auth)):
    """Jellyfin admin username + password. iOS renders these for the user
    to paste into the Jellyfin / Infuse login form."""
    env = ugh_modules.read_module_env("jellyfin")
    # module-data dir for video is called jellyfin, not video. read_module_env
    # keys by Module literal ("video"), but on disk the path is …/module-data/jellyfin.
    # Read the file directly with the correct name.
    env_path = ugh_modules.MODULE_DATA_ROOT / "jellyfin" / ".env"
    if not env_path.exists():
        raise HTTPException(status_code=404, detail="Video module not installed")
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    user = env.get("JELLYFIN_ADMIN_USER")
    password = env.get("JELLYFIN_ADMIN_PASSWORD")
    if not user or not password:
        raise HTTPException(status_code=404, detail="Admin credentials not yet provisioned")
    spec = ugh_modules.REGISTRY["video"]
    return {
        "username": user,
        "password": password,
        "port": spec.default_port,
        "version": env.get("JELLYFIN_VERSION"),
    }


@app.get("/storage/usage-breakdown")
async def storage_usage_breakdown(_user: str = Depends(require_auth)):
    """Per-module bytes + system + free + total. Cached for 60s on the Pi —
    iOS can poll every few seconds without the scan cost.

    Shape matches the iOS `StorageBreakdown` model; keys are stable."""
    return ugh_modules.usage_breakdown()


@app.get("/storage/mount-info")
async def storage_mount_info(_user: str = Depends(require_auth)):
    """Where the user's bulk storage actually lives on the Pi. Other modules'
    install scripts (sprints c+) will hit this to discover the mount point
    rather than parsing /etc/fstab themselves."""
    usage = shutil.disk_usage(str(STORAGE_ROOT))
    fs_type = "unknown"
    try:
        # Cheap parse of /proc/mounts to find the filesystem hosting STORAGE_ROOT.
        # If anything fails we still return mount + sizes — fs_type is informational.
        mount_root = str(STORAGE_ROOT)
        with open("/proc/mounts") as f:
            best_match = ""
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1]
                if mount_root.startswith(mp) and len(mp) > len(best_match):
                    best_match = mp
                    fs_type = parts[2]
    except OSError:
        pass
    return {
        "mount_point": str(STORAGE_ROOT),
        "ugh_root": str(ugh_modules.UGH_ROOT),
        "media_root": str(ugh_modules.MEDIA_ROOT),
        "module_data_root": str(ugh_modules.MODULE_DATA_ROOT),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "filesystem": fs_type,
    }


# ---------------------------------------------------------------------------
# System / OTA updates
# ---------------------------------------------------------------------------

class UpdateApplyRequest(BaseModel):
    git_ref: str


@app.get("/system/version")
async def system_version():
    """Lightweight version endpoint. Cheap — safe to poll."""
    return {
        "version": current_version(),
        "git_sha": current_git_sha(),
    }


@app.get("/system/update/check")
async def system_update_check(_user: str = Depends(require_auth)):
    """Ask the configured manifest URL whether a newer release exists.
    Degrades gracefully when no manifest is configured — iOS hides the
    update CTA based on `configured == false`."""
    return await check_for_update()


@app.post("/system/update/apply")
async def system_update_apply(body: UpdateApplyRequest, _user: str = Depends(require_auth)):
    """Kick off the update worker in a detached process and return immediately.
    iOS polls /system/update/status to track progress — this endpoint must
    return before the worker touches the running service."""
    # Reject obviously-hostile refs before spawning. The shell script also
    # validates, but failing fast here keeps bad inputs out of update.log.
    ref = body.git_ref.strip()
    if not ref or any(ch.isspace() for ch in ref) or any(ch in ref for ch in ";&|$`"):
        raise HTTPException(status_code=400, detail="Invalid git_ref")
    return await trigger_update(ref)


@app.get("/system/update/status")
async def system_update_status(_user: str = Depends(require_auth)):
    """Return the latest state written by update.sh. Polling target during
    an in-flight update; also safe to poll when idle."""
    return read_state()


# ---------------------------------------------------------------------------
# Heartbeat endpoint (unauthenticated, but requires device secret header)
# ---------------------------------------------------------------------------

@app.post("/heartbeat")
async def heartbeat(
    x_device_secret: str = Header(..., alias="X-Device-Secret"),
):
    if not DEVICE_SHARED_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Device not registered",
        )
    if x_device_secret != DEVICE_SHARED_SECRET:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid device secret",
        )

    try:
        result = await send_heartbeat()
        return {"status": "ok", "result": result}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Heartbeat failed: {exc}",
        )


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------

@app.get("/files", response_model=list[FileInfo])
async def list_files(
    path: str = Query("", description="Subdirectory path"),
    limit: int = Query(100, ge=1, le=10000, description="Maximum number of files to return"),
    offset: int = Query(0, ge=0, description="Number of files to skip"),
    favorites_only: bool = Query(False, description="Return only favorited files"),
    recursive: bool = Query(False, description="Include files from all subdirectories"),
    _user: str = Depends(require_auth),
):
    clean = _sanitize_path(path) if path else ""
    db = await get_db()

    if favorites_only:
        # Return all favorited files across all directories
        cursor = await db.execute(
            "SELECT * FROM files WHERE is_favorite = 1 AND is_trashed = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        files = [await _file_row_to_info(r) for r in rows]
        return JSONResponse(
            content={
                "files": [f.model_dump() for f in files],
                "subdirectories": [],
                "path": clean,
            }
        )

    if recursive:
        # Return all files at or under the given path
        prefix = f"{clean}/" if clean else ""
        if clean:
            cursor = await db.execute(
                "SELECT * FROM files WHERE (path = ? OR path LIKE ?) AND is_trashed = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (clean, f"{prefix}%", limit, offset),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM files WHERE is_trashed = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        files = [await _file_row_to_info(r) for r in rows]
        return JSONResponse(
            content={
                "files": [f.model_dump() for f in files],
                "subdirectories": [],
                "path": clean,
            }
        )

    cursor = await db.execute(
        "SELECT * FROM files WHERE path = ? AND is_trashed = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (clean, limit, offset),
    )
    rows = await cursor.fetchall()
    # Also discover subdirectories
    prefix = f"{clean}/" if clean else ""
    cursor2 = await db.execute(
        "SELECT DISTINCT path FROM files WHERE path LIKE ? AND path != ? AND is_trashed = 0",
        (f"{prefix}%", clean),
    )
    sub_rows = await cursor2.fetchall()

    # Compute immediate child directories
    subdirs: set[str] = set()
    for r in sub_rows:
        relative = r["path"]
        if clean:
            relative = relative[len(prefix):]
        top = relative.split("/")[0]
        if top:
            subdirs.add(top)

    files = [await _file_row_to_info(r) for r in rows]
    return JSONResponse(
        content={
            "files": [f.model_dump() for f in files],
            "subdirectories": sorted(subdirs),
            "path": clean,
        }
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.post("/files/upload", response_model=FileInfo)
async def upload_file(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    path: str = Query("", description="Subdirectory to upload into"),
    _user: str = Depends(require_auth),
):
    # Disk-pressure pre-flight: refuse uploads when the drive has less than
    # 1 GB free. Leaves headroom for SQLite WAL, thumbnail generation, and
    # the Immich Postgres WAL — if we accept uploads down to zero free, the
    # whole Pi can hang unrecoverably. 507 is the standard "insufficient
    # storage" status; iOS surfaces the message in the upload queue row.
    if ugh_modules.is_disk_full():
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="Device is nearly full. Free up space before uploading more.",
        )

    clean = _sanitize_path(path) if path else ""
    file_id = str(uuid.uuid4())

    # Determine mime type
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"

    # Build destination
    dest_dir = STORAGE_ROOT / clean if clean else STORAGE_ROOT
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{file_id}_{file.filename}"

    # Stream file to disk in chunks, computing checksum
    sha256 = hashlib.sha256()
    total_size = 0
    chunk_size = 1024 * 1024  # 1 MB

    try:
        with open(dest_path, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    # Clean up and reject
                    f.close()
                    dest_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds maximum size of {MAX_UPLOAD_SIZE} bytes",
                    )
                sha256.update(chunk)
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

    checksum = sha256.hexdigest()
    now = datetime.now(timezone.utc).isoformat()

    # Videos get pre-transcoded to HLS in the background. Non-videos leave
    # hls_status NULL so iOS knows not to wait on anything.
    is_video = mime.startswith("video/")
    initial_hls_status = "pending" if is_video else None

    # Insert metadata
    db = await get_db()
    await db.execute(
        """
        INSERT INTO files (id, filename, path, size, mime_type, created_at, checksum, hls_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, file.filename, clean, total_size, mime, now, checksum, initial_hls_status),
    )
    await db.commit()

    # Generate thumbnail (fire-and-forget-ish, but we await it for correctness)
    await generate_thumbnail(file_id, dest_path, mime)

    # Queue HLS transcode for videos. Runs after the response is sent so the
    # client isn't blocked waiting on ffmpeg (which can take minutes).
    if is_video:
        background_tasks.add_task(transcode_video_to_hls, file_id, dest_path)

    thumb_path = THUMBNAIL_ROOT / f"{file_id}.jpg"
    thumb_url = f"/files/thumbnail/{file_id}" if thumb_path.exists() else None

    return FileInfo(
        id=file_id,
        filename=file.filename or "unknown",
        path=clean,
        size=total_size,
        mime_type=mime,
        created_at=now,
        checksum=checksum,
        thumbnail_url=thumb_url,
        hls_status=initial_hls_status,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.get("/files/download/{file_id}")
async def download_file(file_id: str, _user: str = Depends(require_auth)):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")

    dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    dest_path = dest_dir / f"{row['id']}_{row['filename']}"

    if not dest_path.exists():
        raise HTTPException(status_code=404, detail="File missing from storage")

    return FileResponse(
        path=str(dest_path),
        filename=row["filename"],
        media_type=row["mime_type"],
    )


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

@app.get("/files/thumbnail/{file_id}")
async def get_thumbnail(file_id: str, _user: str = Depends(require_auth)):
    thumb_path = THUMBNAIL_ROOT / f"{file_id}.jpg"
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path=str(thumb_path), media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@app.delete("/files/{file_id}")
async def delete_file(file_id: str, _user: str = Depends(require_auth)):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")

    # Remove actual file
    dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    dest_path = dest_dir / f"{row['id']}_{row['filename']}"
    dest_path.unlink(missing_ok=True)

    # Remove thumbnail
    thumb_path = THUMBNAIL_ROOT / f"{file_id}.jpg"
    thumb_path.unlink(missing_ok=True)

    # Remove HLS output
    await purge_hls(file_id)

    # Remove DB record (CASCADE drops any hls_tokens pointing at this file).
    await db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    await db.commit()

    return {"detail": "File deleted"}


# ---------------------------------------------------------------------------
# Mkdir
# ---------------------------------------------------------------------------

@app.post("/files/mkdir")
async def make_directory(body: MkdirRequest, _user: str = Depends(require_auth)):
    clean = _sanitize_path(body.path)
    if not clean:
        raise HTTPException(status_code=400, detail="Path must not be empty")
    target = STORAGE_ROOT / clean
    target.mkdir(parents=True, exist_ok=True)
    return {"detail": f"Directory created: {clean}"}


# ---------------------------------------------------------------------------
# Batch delete (static route — must be before {file_id} routes)
# ---------------------------------------------------------------------------

@app.post("/files/batch-delete")
async def batch_delete(body: BatchDeleteRequest, _user: str = Depends(require_auth)):
    if not body.ids:
        raise HTTPException(status_code=400, detail="No file IDs provided")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 files per batch")

    db = await get_db()
    deleted_count = 0

    for fid in body.ids:
        cursor = await db.execute("SELECT * FROM files WHERE id = ?", (fid,))
        row = await cursor.fetchone()
        if row is None:
            continue

        # Remove physical file
        dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
        dest_path = dest_dir / f"{row['id']}_{row['filename']}"
        dest_path.unlink(missing_ok=True)

        # Remove thumbnail
        thumb_path = THUMBNAIL_ROOT / f"{row['id']}.jpg"
        thumb_path.unlink(missing_ok=True)

        await db.execute("DELETE FROM files WHERE id = ?", (fid,))
        deleted_count += 1

    # Clean up orphaned share links
    await db.execute(
        "DELETE FROM share_links WHERE file_id NOT IN (SELECT id FROM files)"
    )
    await db.commit()

    return {"detail": f"Deleted {deleted_count} file(s)", "deleted": deleted_count}


# ---------------------------------------------------------------------------
# Trash list & empty (static routes — must be before {file_id} routes)
# ---------------------------------------------------------------------------

@app.get("/files/trash")
async def list_trash(_user: str = Depends(require_auth)):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM files WHERE is_trashed = 1 ORDER BY trashed_at DESC"
    )
    rows = await cursor.fetchall()
    files = [await _file_row_to_info(r) for r in rows]
    return JSONResponse(content=[f.model_dump() for f in files])


@app.post("/files/trash/empty")
async def empty_trash(_user: str = Depends(require_auth)):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM files WHERE is_trashed = 1")
    rows = await cursor.fetchall()

    deleted_count = 0
    for row in rows:
        # Remove physical file
        dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
        dest_path = dest_dir / f"{row['id']}_{row['filename']}"
        dest_path.unlink(missing_ok=True)

        # Remove thumbnail
        thumb_path = THUMBNAIL_ROOT / f"{row['id']}.jpg"
        thumb_path.unlink(missing_ok=True)
        deleted_count += 1

    await db.execute("DELETE FROM files WHERE is_trashed = 1")
    # Also clean up share links for deleted files
    await db.execute(
        "DELETE FROM share_links WHERE file_id NOT IN (SELECT id FROM files)"
    )
    await db.commit()

    return {"detail": f"Emptied trash: {deleted_count} file(s) permanently deleted"}


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

@app.post("/files/{file_id}/rename")
async def rename_file(
    file_id: str,
    body: RenameRequest,
    _user: str = Depends(require_auth),
):
    new_filename = body.filename.strip()
    if not new_filename or "/" in new_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    row = await _get_file_or_404(file_id)
    db = await get_db()

    # Rename physical file on disk
    dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    old_path = dest_dir / f"{row['id']}_{row['filename']}"
    new_path = dest_dir / f"{row['id']}_{new_filename}"

    if old_path.exists():
        old_path.rename(new_path)

    # Update DB
    await db.execute(
        "UPDATE files SET filename = ? WHERE id = ?",
        (new_filename, file_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    updated = await cursor.fetchone()
    return (await _file_row_to_info(updated)).model_dump()


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------

@app.post("/files/{file_id}/move")
async def move_file(
    file_id: str,
    body: MoveRequest,
    _user: str = Depends(require_auth),
):
    new_path = _sanitize_path(body.destination) if body.destination else ""
    row = await _get_file_or_404(file_id)
    db = await get_db()

    old_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    new_dir = STORAGE_ROOT / new_path if new_path else STORAGE_ROOT
    new_dir.mkdir(parents=True, exist_ok=True)

    disk_filename = f"{row['id']}_{row['filename']}"
    old_file = old_dir / disk_filename
    new_file = new_dir / disk_filename

    if old_file.exists():
        shutil.move(str(old_file), str(new_file))

    await db.execute(
        "UPDATE files SET path = ? WHERE id = ?",
        (new_path, file_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    updated = await cursor.fetchone()
    return (await _file_row_to_info(updated)).model_dump()


# ---------------------------------------------------------------------------
# Favorite
# ---------------------------------------------------------------------------

class FavoriteRequest(BaseModel):
    favorite: bool | None = None  # If provided, set explicitly; if omitted, toggle


@app.post("/files/{file_id}/favorite")
async def toggle_favorite(
    file_id: str,
    body: FavoriteRequest | None = None,
    _user: str = Depends(require_auth),
):
    row = await _get_file_or_404(file_id)
    db = await get_db()

    if body is not None and body.favorite is not None:
        # Client sent an explicit value — use it
        new_val = 1 if body.favorite else 0
    else:
        # No body or no explicit value — toggle
        new_val = 0 if row["is_favorite"] else 1

    await db.execute(
        "UPDATE files SET is_favorite = ? WHERE id = ?",
        (new_val, file_id),
    )
    await db.commit()

    return {"id": file_id, "is_favorite": bool(new_val)}


# ---------------------------------------------------------------------------
# Share link
# ---------------------------------------------------------------------------

@app.post("/files/{file_id}/share", response_model=ShareLinkResponse)
async def create_share_link(
    file_id: str,
    body: ShareLinkRequest = ShareLinkRequest(),
    _user: str = Depends(require_auth),
):
    await _get_file_or_404(file_id)
    db = await get_db()

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in)
    link_id = str(uuid.uuid4())

    await db.execute(
        """
        INSERT INTO share_links (id, file_id, token, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (link_id, file_id, token, expires_at.isoformat()),
    )
    await db.commit()

    return ShareLinkResponse(
        url=f"/shared/{token}",
        token=token,
        expires_at=expires_at.isoformat(),
    )


@app.get("/shared/{token}")
async def download_shared_file(token: str):
    """Public endpoint — no auth required. Downloads a file via share token."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM share_links WHERE token = ?", (token,)
    )
    link = await cursor.fetchone()
    if link is None:
        raise HTTPException(status_code=404, detail="Share link not found")

    # Check expiry
    expires = datetime.fromisoformat(link["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        # Clean up expired link
        await db.execute("DELETE FROM share_links WHERE id = ?", (link["id"],))
        await db.commit()
        raise HTTPException(status_code=410, detail="Share link has expired")

    # Fetch the file
    cursor2 = await db.execute(
        "SELECT * FROM files WHERE id = ?", (link["file_id"],)
    )
    row = await cursor2.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File no longer exists")

    dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    dest_path = dest_dir / f"{row['id']}_{row['filename']}"

    if not dest_path.exists():
        raise HTTPException(status_code=404, detail="File missing from storage")

    return FileResponse(
        path=str(dest_path),
        filename=row["filename"],
        media_type=row["mime_type"],
    )


# ---------------------------------------------------------------------------
# Data export — builds a ZIP with every non-trashed file plus a manifest.json
# of metadata, streams it to the client, and cleans up afterwards.
# ---------------------------------------------------------------------------

def _build_export_zip(rows: list[dict]) -> Path:
    """Build an export zip on disk and return the path. Runs in a worker thread."""
    tmp_fd, tmp_path_str = tempfile.mkstemp(prefix="ughstorage-export-", suffix=".zip")
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)

    manifest_files: list[dict] = []

    # ZIP_DEFLATED keeps the archive smaller; media files are already compressed
    # so the overhead is minor and overall payload is meaningfully smaller for
    # documents, text, source trees, etc.
    with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for row in rows:
            if row["is_trashed"]:
                continue
            dest_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
            dest_path = dest_dir / f"{row['id']}_{row['filename']}"
            if not dest_path.exists():
                # Database row without a file on disk — record in manifest and skip.
                manifest_files.append({
                    "id": row["id"],
                    "filename": row["filename"],
                    "path": row["path"],
                    "size": row["size"],
                    "mime_type": row["mime_type"],
                    "created_at": row["created_at"],
                    "is_favorite": bool(row["is_favorite"]) if "is_favorite" in row.keys() else False,
                    "checksum": row["checksum"] if "checksum" in row.keys() else None,
                    "missing": True,
                })
                continue
            # Under "files/<original-path>/<filename>". This keeps the user's
            # folder structure so they can restore into any NAS/filesystem later.
            arcname_dir = Path("files") / (row["path"] or "")
            arcname = str(arcname_dir / row["filename"])
            zf.write(dest_path, arcname=arcname)
            manifest_files.append({
                "id": row["id"],
                "filename": row["filename"],
                "path": row["path"],
                "size": row["size"],
                "mime_type": row["mime_type"],
                "created_at": row["created_at"],
                "is_favorite": bool(row["is_favorite"]) if "is_favorite" in row.keys() else False,
                "checksum": row["checksum"] if "checksum" in row.keys() else None,
                "archive_path": arcname,
            })

        manifest = {
            "export_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(manifest_files),
            "files": manifest_files,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return tmp_path


def _cleanup_export(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to delete export temp file %s", path)


@app.get("/export")
async def export_all(_user: str = Depends(require_auth)):
    """Stream a ZIP of every file plus a manifest.json of metadata."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM files WHERE is_trashed = 0")
    rows = [dict(r) for r in await cursor.fetchall()]

    # Pre-flight free-space check. The ZIP is assembled on disk before streaming,
    # and worst-case (already-compressed media) its size ≈ sum of file sizes.
    # Refuse if we don't have at least 1.2× headroom — anything tighter risks
    # filling the Pi's disk and knocking services offline.
    total_bytes = sum(int(r["size"]) for r in rows)
    tmp_dir = Path(tempfile.gettempdir())
    try:
        free_bytes = shutil.disk_usage(str(tmp_dir)).free
    except Exception:
        free_bytes = 0
    required = int(total_bytes * 1.2)
    if free_bytes and required > free_bytes:
        raise HTTPException(
            status_code=507,  # Insufficient Storage
            detail=(
                f"Not enough free space on device to build an export: "
                f"need ~{required // (1024 * 1024)} MB, have {free_bytes // (1024 * 1024)} MB. "
                "Free up space on the device and try again."
            ),
        )

    zip_path = await asyncio.to_thread(_build_export_zip, rows)

    filename = f"ughstorage-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(
        path=str(zip_path),
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(_cleanup_export, zip_path),
    )


# ---------------------------------------------------------------------------
# HLS streaming — short-lived path-scoped tokens so AVPlayer can fetch segments
# without needing to inject an Authorization header. Token lives in hls_tokens
# with an expiry (default 2 hours); the public /hls/{token}/... route validates
# it and maps to HLS_ROOT/{file_id}/...
# ---------------------------------------------------------------------------

class HLSStreamResponse(BaseModel):
    master_url: str
    token: str
    expires_at: str


@app.post("/files/{file_id}/hls-retry")
async def hls_retry(
    file_id: str,
    background_tasks: BackgroundTasks,
    _user: str = Depends(require_auth),
):
    """Re-queue HLS transcode for a video that previously failed or was skipped."""
    row = await _get_file_or_404(file_id)
    if not row["mime_type"].startswith("video/"):
        raise HTTPException(status_code=400, detail="Not a video")
    src_dir = STORAGE_ROOT / row["path"] if row["path"] else STORAGE_ROOT
    src = src_dir / f"{row['id']}_{row['filename']}"
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source file missing")

    # Reset status so the client sees 'transcoding' immediately.
    db = await get_db()
    await db.execute(
        "UPDATE files SET hls_status = 'pending', hls_error = NULL WHERE id = ?",
        (file_id,),
    )
    await db.commit()
    background_tasks.add_task(transcode_video_to_hls, file_id, src)
    return {"status": "queued"}


@app.post("/files/{file_id}/hls-token", response_model=HLSStreamResponse)
async def create_hls_token(file_id: str, _user: str = Depends(require_auth)):
    row = await _get_file_or_404(file_id)
    hls_status = row["hls_status"] if "hls_status" in row.keys() else None
    if hls_status != "ready":
        raise HTTPException(status_code=409, detail=f"HLS not available (status: {hls_status or 'none'})")
    if not hls_master_path(file_id).exists():
        # DB says ready but files are gone — likely disk was cleaned. Reset status
        # so the client sees "not available" and a future reupload can recover.
        db = await get_db()
        await db.execute("UPDATE files SET hls_status = NULL WHERE id = ?", (file_id,))
        await db.commit()
        raise HTTPException(status_code=404, detail="HLS output missing on disk")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)

    db = await get_db()
    await db.execute(
        "INSERT INTO hls_tokens (token, file_id, expires_at) VALUES (?, ?, ?)",
        (token, file_id, expires_at.isoformat()),
    )
    await db.commit()

    return HLSStreamResponse(
        master_url=f"/hls/{token}/master.m3u8",
        token=token,
        expires_at=expires_at.isoformat(),
    )


def _validate_hls_path(rel_path: str) -> str:
    """Prevent path traversal. Accept only simple HLS artifacts."""
    cleaned = Path(rel_path).as_posix().strip("/")
    if ".." in cleaned.split("/") or cleaned.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    # Only serve .m3u8 and .ts — blocks accidental exposure of anything else.
    if not (cleaned.endswith(".m3u8") or cleaned.endswith(".ts")):
        raise HTTPException(status_code=400, detail="Invalid HLS asset")
    return cleaned


@app.get("/hls/{token}/{rel_path:path}")
async def serve_hls(token: str, rel_path: str):
    """Public endpoint (no auth header). Token in path scopes access to one file's HLS output."""
    cleaned = _validate_hls_path(rel_path)

    db = await get_db()
    cursor = await db.execute(
        "SELECT file_id, expires_at FROM hls_tokens WHERE token = ?", (token,)
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Stream token not found")

    expires = datetime.fromisoformat(row["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        await db.execute("DELETE FROM hls_tokens WHERE token = ?", (token,))
        await db.commit()
        raise HTTPException(status_code=410, detail="Stream token expired")

    asset_path = hls_dir(row["file_id"]) / cleaned
    if not asset_path.exists():
        raise HTTPException(status_code=404, detail="HLS asset missing")

    media_type = "application/vnd.apple.mpegurl" if cleaned.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path=str(asset_path), media_type=media_type)


# ---------------------------------------------------------------------------
# Duplicate detection — groups non-trashed files that share a SHA-256 checksum.
# Encrypted uploads carry a random AES-GCM nonce so their ciphertext hash differs
# per upload; duplicate detection therefore only finds plaintext duplicates.
# ---------------------------------------------------------------------------

class DuplicateGroup(BaseModel):
    checksum: str
    files: list[FileInfo]
    reclaimable_bytes: int


class DuplicatesResponse(BaseModel):
    groups: list[DuplicateGroup]
    total_reclaimable_bytes: int


@app.get("/files/duplicates", response_model=DuplicatesResponse)
async def list_duplicates(_user: str = Depends(require_auth)):
    db = await get_db()
    # Find checksums shared by 2+ non-trashed files.
    cursor = await db.execute(
        """
        SELECT checksum
        FROM files
        WHERE is_trashed = 0 AND checksum IS NOT NULL AND checksum != ''
        GROUP BY checksum
        HAVING COUNT(*) >= 2
        """
    )
    checksums = [row["checksum"] for row in await cursor.fetchall()]

    groups: list[DuplicateGroup] = []
    total_reclaim = 0

    for checksum in checksums:
        cursor = await db.execute(
            "SELECT * FROM files WHERE checksum = ? AND is_trashed = 0 ORDER BY created_at DESC",
            (checksum,),
        )
        rows = await cursor.fetchall()
        if len(rows) < 2:
            continue
        files = [await _file_row_to_info(r) for r in rows]
        # Reclaimable = size × (count - 1) — keeping one copy, deleting the rest.
        per_file_size = files[0].size
        reclaim = per_file_size * (len(files) - 1)
        total_reclaim += reclaim
        groups.append(DuplicateGroup(checksum=checksum, files=files, reclaimable_bytes=reclaim))

    # Largest reclaim first — users want to tackle the heaviest offenders.
    groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    return DuplicatesResponse(groups=groups, total_reclaimable_bytes=total_reclaim)


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

@app.post("/files/{file_id}/trash")
async def trash_file(file_id: str, _user: str = Depends(require_auth)):
    row = await _get_file_or_404(file_id)
    if row["is_trashed"]:
        return {"detail": "File already in trash"}

    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?",
        (now, file_id),
    )
    await db.commit()
    return {"detail": "File moved to trash"}


@app.post("/files/{file_id}/restore")
async def restore_file(file_id: str, _user: str = Depends(require_auth)):
    row = await _get_file_or_404(file_id)
    if not row["is_trashed"]:
        return {"detail": "File is not in trash"}

    db = await get_db()
    await db.execute(
        "UPDATE files SET is_trashed = 0, trashed_at = NULL WHERE id = ?",
        (file_id,),
    )
    await db.commit()
    return {"detail": "File restored"}


# ---------------------------------------------------------------------------
# Storage stats
# ---------------------------------------------------------------------------

@app.get("/storage/stats", response_model=StorageStats)
async def storage_stats(_user: str = Depends(require_auth)):
    usage = shutil.disk_usage(str(STORAGE_ROOT))
    return StorageStats(total=usage.total, used=usage.used, free=usage.free)


# ---------------------------------------------------------------------------
# Format storage
# ---------------------------------------------------------------------------

async def _format_storage() -> None:
    """Delete all files, thumbnails, and recreate the database."""
    # Delete all files in STORAGE_ROOT (but keep the directory)
    for item in STORAGE_ROOT.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Delete all thumbnails in THUMBNAIL_ROOT
    for item in THUMBNAIL_ROOT.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Clear and recreate the database
    await reset_db()


@app.post("/storage/format")
async def format_storage(_user: str = Depends(require_auth)):
    await _format_storage()
    return {"status": "formatted", "message": "All files have been deleted"}


# ---------------------------------------------------------------------------
# Factory reset
# ---------------------------------------------------------------------------

async def _delayed_factory_reset(delay: float = 3.0) -> None:
    """Wait, then run factory_reset(). Intended to run after the response is sent."""
    await asyncio.sleep(delay)
    try:
        await factory_reset()
    except Exception:
        logger.exception("Error during delayed factory reset")


@app.post("/device/factory-reset")
async def device_factory_reset(
    background_tasks: BackgroundTasks,
    _user: str = Depends(require_auth),
):
    # Format storage first
    await _format_storage()
    # Schedule the factory reset to run after the response is returned
    background_tasks.add_task(_delayed_factory_reset, 3.0)
    return {
        "status": "resetting",
        "message": "Factory reset initiated. Device will restart BLE for new setup.",
    }


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------

def _get_system_info() -> dict:
    """Gather system information from the Pi."""
    import psutil

    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "uptime_seconds": 0,
        "cpu_temp": None,
        "cpu_percent": 0.0,
        "memory_total": 0,
        "memory_used": 0,
        "memory_percent": 0.0,
        "ip_address": None,
        "mac_address": None,
    }

    try:
        info["uptime_seconds"] = int(time.time() - psutil.boot_time())
    except Exception:
        pass

    try:
        info["cpu_percent"] = psutil.cpu_percent(interval=0.5)
    except Exception:
        pass

    try:
        mem = psutil.virtual_memory()
        info["memory_total"] = mem.total
        info["memory_used"] = mem.used
        info["memory_percent"] = mem.percent
    except Exception:
        pass

    # CPU temperature (Raspberry Pi specific)
    try:
        temps = psutil.sensors_temperatures()
        if "cpu_thermal" in temps:
            info["cpu_temp"] = temps["cpu_thermal"][0].current
        elif "cpu-thermal" in temps:
            info["cpu_temp"] = temps["cpu-thermal"][0].current
    except Exception:
        pass

    # Get IP and MAC from default network interface
    try:
        addrs = psutil.net_if_addrs()
        for iface in ("wlan0", "eth0", "en0"):
            if iface in addrs:
                for addr in addrs[iface]:
                    if addr.family == socket.AF_INET:
                        info["ip_address"] = addr.address
                    elif addr.family == psutil.AF_LINK:
                        info["mac_address"] = addr.address
                if info["ip_address"]:
                    break
    except Exception:
        pass

    return info


@app.get("/device/info")
async def device_info(_user: str = Depends(require_auth)):
    """Return detailed device information including network, hardware, and WiFi status."""
    from wifi_manager import get_status as wifi_get_status

    sys_info = _get_system_info()

    # Get WiFi status
    try:
        wifi = wifi_get_status()
        sys_info["wifi_ssid"] = wifi.ssid
        sys_info["wifi_connected"] = wifi.connected
        sys_info["wifi_ip"] = wifi.ip_address
        sys_info["wifi_mac"] = wifi.mac_address
    except Exception:
        sys_info["wifi_ssid"] = None
        sys_info["wifi_connected"] = False

    sys_info["device_id"] = DEVICE_ID
    sys_info["server_version"] = "2.0.0"

    return sys_info


# ---------------------------------------------------------------------------
# WiFi management
# ---------------------------------------------------------------------------

@app.get("/device/wifi/status")
async def wifi_status(_user: str = Depends(require_auth)):
    from wifi_manager import get_status as wifi_get_status
    status = wifi_get_status()
    return status.to_dict()


@app.get("/device/wifi/scan")
async def wifi_scan(_user: str = Depends(require_auth)):
    from wifi_manager import scan_networks
    networks = scan_networks()
    return {"networks": networks}


@app.post("/device/wifi/connect")
async def wifi_connect(
    body: WiFiConnectRequest,
    _user: str = Depends(require_auth),
):
    from wifi_manager import connect as wifi_connect_fn
    success, message = wifi_connect_fn(body.ssid, body.password)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "connected", "message": message}


# ---------------------------------------------------------------------------
# Device rename
# ---------------------------------------------------------------------------

@app.post("/device/rename")
async def rename_device(
    body: DeviceRenameRequest,
    _user: str = Depends(require_auth),
):
    new_name = body.name.strip()
    if not new_name or len(new_name) > 64:
        raise HTTPException(status_code=400, detail="Invalid device name (1-64 characters)")

    # Update hostname on the Pi
    try:
        import subprocess
        subprocess.run(["hostnamectl", "set-hostname", new_name], check=True, timeout=10)
    except Exception as exc:
        logger.warning("Failed to set hostname: %s", exc)
        # Non-fatal — continue to update Supabase record

    return {"status": "renamed", "name": new_name}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
