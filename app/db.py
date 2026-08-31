"""SQLite storage: settings, watch history, and the cached library media-info tables."""
import json
import aiosqlite
from typing import Any

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    user TEXT,
    user_id TEXT,
    player TEXT,
    platform TEXT,
    product TEXT,
    address TEXT,
    location TEXT,
    media_type TEXT,
    rating_key TEXT,
    section_id TEXT,
    title TEXT,
    parent_title TEXT,
    grandparent_title TEXT,
    year INTEGER,
    started_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    stopped_at INTEGER,
    state TEXT,
    view_offset_ms INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    progress_pct REAL DEFAULT 0,
    max_progress_pct REAL DEFAULT 0,
    bitrate_kbps INTEGER,
    stream_decision TEXT,
    video_decision TEXT,
    audio_decision TEXT,
    quality TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_started ON history(started_at);
CREATE INDEX IF NOT EXISTS idx_history_user ON history(user);
CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_key, stopped_at);

CREATE TABLE IF NOT EXISTS media_items (
    rating_key TEXT PRIMARY KEY,
    section_id TEXT NOT NULL,
    media_type TEXT,
    title TEXT,
    sort_title TEXT,
    parent_title TEXT,
    grandparent_title TEXT,
    year INTEGER,
    added_at INTEGER,
    duration_ms INTEGER,
    container TEXT,
    video_codec TEXT,
    video_resolution TEXT,
    video_profile TEXT,
    audio_codec TEXT,
    audio_channels REAL,
    bitrate_kbps INTEGER,
    file_size INTEGER,
    file_path TEXT,
    view_count INTEGER DEFAULT 0,
    audio_summary TEXT,
    synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_section ON media_items(section_id);

CREATE TABLE IF NOT EXISTS audio_streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rating_key TEXT NOT NULL,
    stream_index INTEGER,
    codec TEXT,
    channels REAL,
    channel_layout TEXT,
    language TEXT,
    title TEXT,
    is_default INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audio_rating ON audio_streams(rating_key);

CREATE TABLE IF NOT EXISTS sync_status (
    section_id TEXT PRIMARY KEY,
    state TEXT,
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    error TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT,
    recipients TEXT,
    sent_at INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT
);
"""

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA)
        await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def get_setting(key: str, default: Any = None) -> Any:
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


async def set_setting(key: str, value: Any) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    await db.commit()


async def log_sent(kind: str, subject: str, recipients: list[str], ok: bool, error: str = "", sent_at: int = 0) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO sent_log(kind, subject, recipients, sent_at, ok, error) VALUES(?,?,?,?,?,?)",
        (kind, subject, ", ".join(recipients), sent_at, 1 if ok else 0, error),
    )
    await db.commit()
