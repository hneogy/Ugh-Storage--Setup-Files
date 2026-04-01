"""SQLite database setup and helpers using aiosqlite."""

import aiosqlite

from config import DATABASE_PATH

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return the shared database connection, creating it if needed."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(str(DATABASE_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _init_tables(_db)
    return _db


async def _init_tables(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            path        TEXT NOT NULL DEFAULT '',
            size        INTEGER NOT NULL,
            mime_type   TEXT NOT NULL DEFAULT 'application/octet-stream',
            created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
            checksum    TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_path ON files (path)"
    )

    # --- Migrations: add columns if they don't exist ---
    await _migrate_add_column(db, "files", "is_favorite", "BOOLEAN NOT NULL DEFAULT 0")
    await _migrate_add_column(db, "files", "is_trashed", "BOOLEAN NOT NULL DEFAULT 0")
    await _migrate_add_column(db, "files", "trashed_at", "DATETIME")

    # Share links table
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
            id          TEXT PRIMARY KEY,
            file_id     TEXT NOT NULL,
            token       TEXT NOT NULL UNIQUE,
            expires_at  DATETIME NOT NULL,
            created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_share_links_token ON share_links (token)"
    )

    await db.commit()


async def _migrate_add_column(
    db: aiosqlite.Connection, table: str, column: str, definition: str
) -> None:
    """Add a column to a table if it doesn't already exist."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in await cursor.fetchall()]
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def reset_db() -> None:
    """Close the DB, delete the file, and reinitialize."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
    DATABASE_PATH.unlink(missing_ok=True)
    # Reinitialize
    await get_db()
