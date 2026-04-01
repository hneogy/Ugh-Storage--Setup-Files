"""Thumbnail generation for images and videos."""

import asyncio
import logging
from pathlib import Path

from PIL import Image

from config import FFMPEG_PATH, THUMBNAIL_MAX_DIMENSION, THUMBNAIL_ROOT

logger = logging.getLogger(__name__)


def _image_thumbnail(source: Path, dest: Path) -> None:
    """Generate a JPEG thumbnail from an image file (blocking)."""
    with Image.open(source) as img:
        img.thumbnail((THUMBNAIL_MAX_DIMENSION, THUMBNAIL_MAX_DIMENSION))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(dest, "JPEG", quality=80)


async def _video_thumbnail(source: Path, dest: Path) -> None:
    """Extract the first frame of a video via ffmpeg and save as JPEG."""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_PATH,
        "-i", str(source),
        "-vframes", "1",
        "-vf", f"scale='min({THUMBNAIL_MAX_DIMENSION},iw)':-1",
        "-y",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("ffmpeg thumbnail failed for %s: %s", source, stderr.decode(errors="replace"))
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")


async def generate_thumbnail(file_id: str, source: Path, mime_type: str) -> Path | None:
    """
    Generate a thumbnail and return its path, or None if the type is unsupported.
    The thumbnail is stored at THUMBNAIL_ROOT/<file_id>.jpg.
    """
    dest = THUMBNAIL_ROOT / f"{file_id}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mime_type.startswith("image/"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _image_thumbnail, source, dest)
            return dest

        if mime_type.startswith("video/"):
            await _video_thumbnail(source, dest)
            return dest

    except Exception:
        logger.exception("Thumbnail generation failed for %s", source)
        if dest.exists():
            dest.unlink(missing_ok=True)

    return None
