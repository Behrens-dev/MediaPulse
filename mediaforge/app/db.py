"""SQLite storage: settings and the ffmpeg job queue."""
import json
import time
from typing import Any

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- downmix / embed_sub / remove_audio / audio_sync / sub_sync
    title TEXT,                     -- display title of the media item
    rating_key TEXT,
    plex_path TEXT NOT NULL,        -- file path as Plex reports it
    input_path TEXT NOT NULL,       -- path after mapping, as the executor sees it
    output_path TEXT NOT NULL,
    suffix TEXT NOT NULL,
    options TEXT NOT NULL,          -- json blob of job options
    mode TEXT NOT NULL,             -- local / ssh (captured when queued)
    status TEXT NOT NULL DEFAULT 'queued',  -- queued / scheduled / running / done / error / canceled
    progress REAL DEFAULT 0,
    log TEXT DEFAULT '',
    error TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    run_at INTEGER,                 -- epoch seconds; only for status 'scheduled'
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# columns added after the first release; applied to existing databases on startup
MIGRATIONS = [
    ("jobs", "run_at", "ALTER TABLE jobs ADD COLUMN run_at INTEGER"),
]

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            async with _db.execute(f"PRAGMA table_info({table})") as cur:
                cols = {r["name"] for r in await cur.fetchall()}
            if column not in cols:
                await _db.execute(ddl)
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


# ---------- jobs ----------

async def create_job(kind: str, title: str, rating_key: str, plex_path: str,
                     input_path: str, output_path: str, suffix: str,
                     options: dict, mode: str, duration_ms: int,
                     status: str = "queued", run_at: int | None = None) -> int:
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO jobs(kind, title, rating_key, plex_path, input_path, output_path,
                            suffix, options, mode, duration_ms, status, run_at, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, title, rating_key, plex_path, input_path, output_path,
         suffix, json.dumps(options), mode, duration_ms, status, run_at, int(time.time())),
    )
    await db.commit()
    return cur.lastrowid


async def get_job(job_id: int) -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        return await cur.fetchone()


async def list_jobs(limit: int = 100) -> list[aiosqlite.Row]:
    db = await get_db()
    async with db.execute(
        "SELECT id, kind, title, plex_path, output_path, mode, status, progress, error, "
        "run_at, created_at, started_at, finished_at FROM jobs ORDER BY id DESC LIMIT ?",
        (limit,)
    ) as cur:
        return await cur.fetchall()


async def release_due_jobs() -> int:
    """Promote scheduled jobs whose start time has arrived into the queue."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE jobs SET status = 'queued' WHERE status = 'scheduled' AND run_at <= ?",
        (int(time.time()),),
    )
    await db.commit()
    return cur.rowcount


async def next_queued_job() -> aiosqlite.Row | None:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
    ) as cur:
        return await cur.fetchone()


async def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    db = await get_db()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
    await db.commit()


async def delete_job(job_id: int) -> None:
    db = await get_db()
    await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
