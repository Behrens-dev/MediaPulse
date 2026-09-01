"""Library media-info sync: caches file/codec/audio-track details for every item
in a library section so the UI can filter on them instantly (and offline)."""
import asyncio
import logging
import time

from . import db, plex

log = logging.getLogger("mediapulse.sync")

# which leaf item type carries the files for each section type
LEAF_TYPE = {
    "movie": plex.TYPE_MOVIE,
    "show": plex.TYPE_EPISODE,
    "artist": plex.TYPE_TRACK,
}

BATCH = 25          # rating keys per /library/metadata request
PAGE = 500          # items per section listing page

_running: set[str] = set()


def is_running(section_id: str) -> bool:
    return section_id in _running


async def _set_status(section_id: str, state: str, total: int = 0, done: int = 0, error: str = "") -> None:
    conn = await db.get_db()
    await conn.execute(
        """INSERT INTO sync_status(section_id, state, total, done, error, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(section_id) DO UPDATE SET
             state=excluded.state, total=excluded.total, done=excluded.done,
             error=excluded.error, updated_at=excluded.updated_at""",
        (section_id, state, total, done, error, int(time.time())),
    )
    await conn.commit()


def _audio_summary(streams: list[dict]) -> str:
    """Human-readable one-liner, e.g. 'TRUEHD 7.1 / AC3 5.1 / AAC 2.0'."""
    parts = []
    for st in streams:
        codec = (st.get("codec") or "?").upper()
        ch = st.get("channels")
        ch_str = f"{ch:g}" if isinstance(ch, (int, float)) else "?"
        layout = {8: "7.1", 7: "6.1", 6: "5.1", 3: "2.1", 2: "2.0", 1: "1.0"}.get(ch, ch_str)
        parts.append(f"{codec} {layout}")
    return " / ".join(parts) if parts else "none"


async def _store_item(conn, section_id: str, item: dict, now: int) -> None:
    media = (item.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]
    streams = part.get("Stream") or []
    audio = [st for st in streams if st.get("streamType") == 2]

    rating_key = str(item.get("ratingKey"))
    await conn.execute(
        """INSERT OR REPLACE INTO media_items
           (rating_key, section_id, media_type, title, sort_title, parent_title,
            grandparent_title, year, added_at, duration_ms, container, video_codec,
            video_resolution, video_profile, audio_codec, audio_channels, bitrate_kbps,
            file_size, file_path, view_count, audio_summary, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rating_key, section_id, item.get("type"),
            item.get("title"), item.get("titleSort") or item.get("title"),
            item.get("parentTitle"), item.get("grandparentTitle"),
            item.get("year"), item.get("addedAt"), item.get("duration"),
            media.get("container") or part.get("container"),
            media.get("videoCodec"), media.get("videoResolution"),
            media.get("videoProfile"),
            media.get("audioCodec"), media.get("audioChannels"),
            media.get("bitrate"), part.get("size"), part.get("file"),
            item.get("viewCount") or 0,
            _audio_summary(audio), now,
        ),
    )
    await conn.execute("DELETE FROM audio_streams WHERE rating_key = ?", (rating_key,))
    for st in audio:
        await conn.execute(
            """INSERT INTO audio_streams
               (rating_key, stream_index, codec, channels, channel_layout, language, title, is_default)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                rating_key, st.get("index"), (st.get("codec") or "").lower(),
                st.get("channels"), st.get("audioChannelLayout"),
                st.get("language") or st.get("languageTag") or "",
                st.get("displayTitle") or st.get("title") or "",
                1 if st.get("default") else 0,
            ),
        )


async def sync_section(section_id: str, section_type: str) -> None:
    """Full media-info sync for one library section. Runs as a background task."""
    if section_id in _running:
        return
    leaf = LEAF_TYPE.get(section_type)
    if leaf is None:
        await _set_status(section_id, "error", error=f"Unsupported library type: {section_type}")
        return

    _running.add(section_id)
    try:
        client = await plex.get_client()
        await _set_status(section_id, "listing")

        # 1) page through the section to collect every leaf rating key
        keys: list[str] = []
        start, total = 0, None
        while total is None or start < total:
            mc = await client.section_items(section_id, leaf, start, PAGE)
            total = int(mc.get("totalSize", 0))
            batch = mc.get("Metadata", [])
            if not batch:
                break
            keys.extend(str(m["ratingKey"]) for m in batch if m.get("ratingKey"))
            start += len(batch)

        await _set_status(section_id, "syncing", total=len(keys), done=0)
        conn = await db.get_db()
        now = int(time.time())

        # 2) fetch full metadata (with Stream elements) in batches
        done = 0
        for i in range(0, len(keys), BATCH):
            chunk = keys[i:i + BATCH]
            items = await client.metadata_batch(chunk)
            for item in items:
                await _store_item(conn, section_id, item, now)
            done += len(chunk)
            await conn.commit()
            if done % (BATCH * 4) == 0 or done >= len(keys):
                await _set_status(section_id, "syncing", total=len(keys), done=done)
            await asyncio.sleep(0.05)  # be gentle with the server

        # 3) drop items that no longer exist in Plex
        placeholders = ",".join("?" * len(keys)) if keys else "''"
        await conn.execute(
            f"DELETE FROM audio_streams WHERE rating_key IN "
            f"(SELECT rating_key FROM media_items WHERE section_id=? AND synced_at < ?)",
            (section_id, now),
        )
        await conn.execute(
            "DELETE FROM media_items WHERE section_id=? AND synced_at < ?", (section_id, now)
        )
        await conn.commit()

        await _set_status(section_id, "done", total=len(keys), done=len(keys))
        log.info("synced section %s: %d items", section_id, len(keys))
    except Exception as e:
        log.exception("sync failed for section %s", section_id)
        await _set_status(section_id, "error", error=str(e))
    finally:
        _running.discard(section_id)
