"""HLS pre-transcode for uploaded videos.

Produces a single-variant HLS ladder (720p H.264 + AAC audio) so that iOS
AVPlayer can stream without downloading the full file. Single variant keeps
Pi 5 CPU manageable; we can grow to a multi-bitrate ladder later once real
devices are in the field.

Output layout:
    HLS_ROOT/{file_id}/master.m3u8      — top-level manifest referencing one variant
    HLS_ROOT/{file_id}/v0/playlist.m3u8 — variant playlist
    HLS_ROOT/{file_id}/v0/seg_000.ts    — MPEG-TS segments (6s each)
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from config import FFMPEG_PATH, HLS_ROOT
from database import get_db

logger = logging.getLogger(__name__)


HLS_VARIANT_HEIGHT = 720
HLS_VARIANT_BITRATE = "2500k"
HLS_AUDIO_BITRATE = "128k"
HLS_SEGMENT_SECONDS = 6

# Pi 5 has 4 performance cores. Two concurrent ffmpeg workers at -veryfast keeps
# the device responsive (uvicorn, Caddy, BLE service) while still draining the
# transcode queue at a reasonable rate. Without this, an upload burst spawns
# one ffmpeg per video and the box becomes unresponsive.
HLS_TRANSCODE_CONCURRENCY = int(os.getenv("UGHSTORAGE_HLS_CONCURRENCY", "2"))
_hls_semaphore = asyncio.Semaphore(HLS_TRANSCODE_CONCURRENCY)


def hls_dir(file_id: str) -> Path:
    return HLS_ROOT / file_id


def hls_master_path(file_id: str) -> Path:
    return hls_dir(file_id) / "master.m3u8"


async def _set_status(file_id: str, status: str, error: str | None = None) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE files SET hls_status = ?, hls_error = ? WHERE id = ?",
        (status, error, file_id),
    )
    await db.commit()


async def _is_encrypted_upload(source: Path) -> bool:
    """Client-encrypted files carry the 5-byte 'UGHE\\x01' header. Skip HLS for those —
    ffmpeg would otherwise spin on the ciphertext and waste minutes of CPU."""
    try:
        with open(source, "rb") as f:
            header = f.read(5)
        return header == b"UGHE\x01"
    except OSError:
        return False


async def transcode_video_to_hls(file_id: str, source: Path) -> None:
    """Transcode a single video to an HLS variant. Updates hls_status in DB
    as it progresses. Designed to run as a fire-and-forget background task —
    callers should not await anything dependent on this returning.

    Bounded by `_hls_semaphore` (default 2 concurrent) so a burst of video
    uploads doesn't fork enough ffmpeg processes to thrash the Pi.
    """
    # Reject empty / vanished source files before we acquire the semaphore so
    # we don't waste a slot on a guaranteed failure.
    try:
        if not source.exists() or source.stat().st_size == 0:
            await _set_status(file_id, "failed", "source missing or empty")
            return
    except OSError as exc:
        await _set_status(file_id, "failed", f"source stat failed: {exc}")
        return

    if await _is_encrypted_upload(source):
        await _set_status(file_id, "unsupported", "encrypted upload")
        return

    # Mark queued so iOS can show "waiting in transcode queue" rather than the
    # generic "pending" while another video is hogging the semaphore.
    await _set_status(file_id, "queued")
    async with _hls_semaphore:
        await _transcode_video_to_hls_inner(file_id, source)


async def _transcode_video_to_hls_inner(file_id: str, source: Path) -> None:
    dest_dir = hls_dir(file_id)
    variant_dir = dest_dir / "v0"

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        variant_dir.mkdir(parents=True, exist_ok=True)

        await _set_status(file_id, "transcoding")

        playlist = variant_dir / "playlist.m3u8"
        segment_template = variant_dir / "seg_%03d.ts"

        # -vf: scale so height is capped at 720 while preserving aspect ratio
        #       and ensuring even dimensions (required by H.264).
        # -hls_flags independent_segments + delete_segments removed: we want
        #       stable segment names since the file is static.
        # -movflags is for fragmented MP4 — not used here since we output TS.
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_PATH,
            "-y",
            "-i", str(source),
            "-c:v", "libx264",
            "-preset", "veryfast",        # optimize for Pi CPU, acceptable quality hit
            "-b:v", HLS_VARIANT_BITRATE,
            "-maxrate", HLS_VARIANT_BITRATE,
            "-bufsize", "5000k",
            "-vf", f"scale=-2:'min({HLS_VARIANT_HEIGHT},ih)'",
            "-c:a", "aac",
            "-b:a", HLS_AUDIO_BITRATE,
            "-ac", "2",
            "-hls_time", str(HLS_SEGMENT_SECONDS),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(segment_template),
            "-f", "hls",
            str(playlist),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            snippet = stderr.decode(errors="replace")[-400:] if stderr else ""
            logger.warning("HLS transcode failed for %s: %s", file_id, snippet)
            await _set_status(file_id, "failed", snippet)
            shutil.rmtree(dest_dir, ignore_errors=True)
            return

        # Write a one-variant master manifest. Keeping a master (rather than
        # pointing AVPlayer at the variant playlist directly) lets us add an
        # adaptive ladder later without changing client URLs.
        master = hls_master_path(file_id)
        master.write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            f"#EXT-X-STREAM-INF:BANDWIDTH=2628000,RESOLUTION=1280x{HLS_VARIANT_HEIGHT}\n"
            "v0/playlist.m3u8\n"
        )
        await _set_status(file_id, "ready")
    except Exception as exc:
        logger.exception("HLS transcode errored for %s", file_id)
        await _set_status(file_id, "failed", str(exc)[:400])
        shutil.rmtree(dest_dir, ignore_errors=True)


async def purge_hls(file_id: str) -> None:
    """Remove on-disk HLS output for a file. Safe to call when HLS was never generated."""
    shutil.rmtree(hls_dir(file_id), ignore_errors=True)
