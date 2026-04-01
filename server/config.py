"""Configuration for UghStorage server, loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Storage ---
STORAGE_ROOT = Path(os.getenv("UGHSTORAGE_STORAGE_ROOT", "/mnt/nvme/storage"))
THUMBNAIL_ROOT = Path(os.getenv("UGHSTORAGE_THUMBNAIL_ROOT", "/mnt/nvme/thumbnails"))
DATABASE_PATH = Path(os.getenv("UGHSTORAGE_DATABASE_PATH", "/mnt/nvme/ughstorage.db"))

# --- Supabase ---
SUPABASE_URL = os.getenv("UGHSTORAGE_SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("UGHSTORAGE_SUPABASE_ANON_KEY", "")

# --- Device identity (populated during BLE registration) ---
DEVICE_ID = os.getenv("UGHSTORAGE_DEVICE_ID", "")
DEVICE_SHARED_SECRET = os.getenv("UGHSTORAGE_DEVICE_SHARED_SECRET", "")

# --- Auth ---
JWT_ALGORITHM = "HS256"

# --- Server ---
HOST = os.getenv("UGHSTORAGE_HOST", "0.0.0.0")
PORT = int(os.getenv("UGHSTORAGE_PORT", "8000"))
MAX_UPLOAD_SIZE = int(os.getenv("UGHSTORAGE_MAX_UPLOAD_SIZE", str(5 * 1024 * 1024 * 1024)))  # 5 GB
CORS_ORIGINS = os.getenv("UGHSTORAGE_CORS_ORIGINS", "*").split(",")

# --- Thumbnails ---
THUMBNAIL_MAX_DIMENSION = int(os.getenv("UGHSTORAGE_THUMBNAIL_MAX_DIM", "300"))
FFMPEG_PATH = os.getenv("UGHSTORAGE_FFMPEG_PATH", "ffmpeg")
