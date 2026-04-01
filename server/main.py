"""UghStorage -- personal cloud storage server (multi-tenant)."""

import asyncio
import hashlib
import logging
import mimetypes
import platform
import secrets
import shutil
import socket
import time
import uuid
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

from auth import require_auth
from config import (
    CORS_ORIGINS,
    DEVICE_ID,
    DEVICE_SHARED_SECRET,
    HOST,
    MAX_UPLOAD_SIZE,
    PORT,
    STORAGE_ROOT,
    THUMBNAIL_ROOT,
)
from database import close_db, get_db, reset_db
from registration import factory_reset, send_heartbeat
from thumbnails import generate_thumbnail

logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: ensure directories exist
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    THUMBNAIL_ROOT.mkdir(parents=True, exist_ok=True)
    # Ensure DB is initialized
    await get_db()
    yield
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
    return {
        "status": "ok",
        "device_id": DEVICE_ID,
        "version": "2.0.0",
    }


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
    _user: str = Depends(require_auth),
):
    clean = _sanitize_path(path) if path else ""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM files WHERE path = ? AND is_trashed = 0 ORDER BY created_at DESC",
        (clean,),
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
    path: str = Query("", description="Subdirectory to upload into"),
    _user: str = Depends(require_auth),
):
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

    # Insert metadata
    db = await get_db()
    await db.execute(
        """
        INSERT INTO files (id, filename, path, size, mime_type, created_at, checksum)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, file.filename, clean, total_size, mime, now, checksum),
    )
    await db.commit()

    # Generate thumbnail (fire-and-forget-ish, but we await it for correctness)
    await generate_thumbnail(file_id, dest_path, mime)

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

    # Remove DB record
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

@app.post("/files/{file_id}/favorite")
async def toggle_favorite(file_id: str, _user: str = Depends(require_auth)):
    row = await _get_file_or_404(file_id)
    db = await get_db()

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
