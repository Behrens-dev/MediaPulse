"""Background pollers: live session tracking -> watch history, and server health -> alerts."""
import asyncio
import logging
import time

from . import db, media_sync, plex, notify
from .config import SESSION_POLL_SECONDS, HEALTH_POLL_SECONDS, HEALTH_FAILURE_THRESHOLD

log = logging.getLogger("mediapulse.poller")


def parse_session(s: dict) -> dict:
    """Flatten one Plex session Metadata object into the fields we track."""
    user = s.get("User", {})
    player = s.get("Player", {})
    session = s.get("Session", {})
    ts = s.get("TranscodeSession")
    media = (s.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]

    duration = int(s.get("duration") or 0)
    offset = int(s.get("viewOffset") or 0)
    progress = round(offset / duration * 100, 1) if duration else 0.0

    if ts:
        decision = "transcode"
        video_decision = ts.get("videoDecision", "transcode")
        audio_decision = ts.get("audioDecision", "transcode")
    else:
        decision = part.get("decision") or "direct play"
        if decision == "directplay":
            decision = "direct play"
        video_decision = decision
        audio_decision = decision

    bitrate = session.get("bandwidth") or media.get("bitrate") or 0

    return {
        "session_key": str(s.get("sessionKey", "")),
        "user": user.get("title", "Unknown"),
        "user_id": str(user.get("id", "")),
        "player": player.get("title") or player.get("product", ""),
        "platform": player.get("platform", ""),
        "product": player.get("product", ""),
        "address": player.get("address", ""),
        "location": session.get("location", ""),
        "media_type": s.get("type", ""),
        "rating_key": str(s.get("ratingKey", "")),
        "section_id": str(s.get("librarySectionID", "")),
        "title": s.get("title", ""),
        "parent_title": s.get("parentTitle", ""),
        "grandparent_title": s.get("grandparentTitle", ""),
        "year": s.get("year"),
        "state": player.get("state", "unknown"),
        "view_offset_ms": offset,
        "duration_ms": duration,
        "progress_pct": progress,
        "bitrate_kbps": int(bitrate),
        "stream_decision": decision,
        "video_decision": video_decision,
        "audio_decision": audio_decision,
        "quality": f"{media.get('videoResolution', '')}".strip(),
        "thumb": s.get("grandparentThumb") or s.get("thumb", ""),
        "transcode_progress": round(float(ts.get("progress", 0)), 1) if ts else None,
        "transcode_speed": ts.get("speed") if ts else None,
    }


class ActivityPoller:
    """Polls /status/sessions and maintains open history rows keyed by sessionKey."""

    def __init__(self):
        self.active: dict[str, int] = {}  # session_key -> history row id
        self.last_sessions: list[dict] = []
        self.last_poll_ok = False
        self.last_error = ""

    async def poll_once(self) -> None:
        client = await plex.get_client()
        if not client.configured:
            return
        try:
            raw = await client.sessions()
            self.last_poll_ok = True
            self.last_error = ""
        except plex.PlexError as e:
            self.last_poll_ok = False
            self.last_error = str(e)
            return

        now = int(time.time())
        parsed = [parse_session(s) for s in raw]
        self.last_sessions = parsed
        conn = await db.get_db()
        seen = set()

        for p in parsed:
            sk = p["session_key"]
            if not sk:
                continue
            seen.add(sk)
            if sk not in self.active:
                cur = await conn.execute(
                    """INSERT INTO history
                       (session_key, user, user_id, player, platform, product, address, location,
                        media_type, rating_key, section_id, title, parent_title, grandparent_title,
                        year, started_at, last_seen_at, state, view_offset_ms, duration_ms,
                        progress_pct, max_progress_pct, bitrate_kbps, stream_decision,
                        video_decision, audio_decision, quality)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sk, p["user"], p["user_id"], p["player"], p["platform"], p["product"],
                     p["address"], p["location"], p["media_type"], p["rating_key"], p["section_id"],
                     p["title"], p["parent_title"], p["grandparent_title"], p["year"], now, now,
                     p["state"], p["view_offset_ms"], p["duration_ms"], p["progress_pct"],
                     p["progress_pct"], p["bitrate_kbps"], p["stream_decision"],
                     p["video_decision"], p["audio_decision"], p["quality"]),
                )
                self.active[sk] = cur.lastrowid
            else:
                await conn.execute(
                    """UPDATE history SET last_seen_at=?, state=?, view_offset_ms=?, duration_ms=?,
                       progress_pct=?, max_progress_pct=MAX(max_progress_pct, ?), bitrate_kbps=?,
                       stream_decision=?, video_decision=?, audio_decision=?, quality=?
                       WHERE id=?""",
                    (now, p["state"], p["view_offset_ms"], p["duration_ms"], p["progress_pct"],
                     p["progress_pct"], p["bitrate_kbps"], p["stream_decision"],
                     p["video_decision"], p["audio_decision"], p["quality"], self.active[sk]),
                )

        # finalize sessions that disappeared
        for sk in list(self.active):
            if sk not in seen:
                await conn.execute(
                    "UPDATE history SET stopped_at=last_seen_at, state='stopped' WHERE id=?",
                    (self.active[sk],),
                )
                del self.active[sk]
        await conn.commit()

    async def close_stale_on_startup(self) -> None:
        """Any rows left open by a previous run are dead sessions — close them."""
        conn = await db.get_db()
        await conn.execute(
            "UPDATE history SET stopped_at=last_seen_at, state='stopped' WHERE stopped_at IS NULL"
        )
        await conn.commit()

    async def run(self) -> None:
        await self.close_stale_on_startup()
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("activity poll failed")
            await asyncio.sleep(SESSION_POLL_SECONDS)


class HealthMonitor:
    """Pings the Plex server; emails the admin when it goes down / recovers, and can
    also auto-send the family-facing outage notice after a configurable delay."""

    def __init__(self):
        self.failures = 0
        self.is_down = False
        self.down_since: int | None = None
        self.outage_sent = False
        self.last_ok_at: int | None = None
        self.last_error = ""

    async def check_once(self) -> None:
        client = await plex.get_client()
        if not client.configured:
            return
        try:
            await client.server_info()
            self.last_ok_at = int(time.time())
            self.last_error = ""
            if self.is_down:
                self.is_down = False
                self.down_since = None
                self.outage_sent = False
                self.failures = 0
                if await db.get_setting("alert_server_down", True):
                    await notify.send_server_status(down=False)
            self.failures = 0
        except plex.PlexError as e:
            self.failures += 1
            self.last_error = str(e)
            if not self.is_down and self.failures >= HEALTH_FAILURE_THRESHOLD:
                self.is_down = True
                self.down_since = int(time.time())
                self.outage_sent = False
                if await db.get_setting("alert_server_down", True):
                    await notify.send_server_status(down=True, error=str(e))
            await self._maybe_auto_outage()

    async def _maybe_auto_outage(self) -> None:
        """Send the outage notice to everyone once the server has been down long enough."""
        if not self.is_down or self.outage_sent or self.down_since is None:
            return
        if not await db.get_setting("outage_auto_enabled", False):
            return
        delay_min = int(await db.get_setting("outage_auto_delay_min", 15))
        if time.time() - self.down_since < delay_min * 60:
            return
        self.outage_sent = True  # one notice per outage, even if the send fails
        message = await db.get_setting("outage_auto_message", "")
        try:
            # never attaches an image — only the saved message rides along
            await notify.send_outage(message=message, auto=True)
            log.info("automatic outage notice sent (down for %d+ min)", delay_min)
        except notify.NotifyError as e:
            log.warning("automatic outage notice failed: %s", e)

    async def run(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(HEALTH_POLL_SECONDS)


class NewsletterScheduler:
    """Sends the recently-added newsletter once a month at the configured day/hour."""

    async def tick(self) -> None:
        if not await db.get_setting("newsletter_enabled", False):
            return
        day = int(await db.get_setting("newsletter_day", 1))
        hour = int(await db.get_setting("newsletter_hour", 9))
        now = time.localtime()
        if now.tm_mday != day or now.tm_hour != hour:
            return
        month_tag = f"{now.tm_year}-{now.tm_mon:02d}"
        if await db.get_setting("newsletter_last_sent", "") == month_tag:
            return
        days_back = int(await db.get_setting("newsletter_days_back", 30))
        try:
            await notify.send_newsletter(days_back=days_back, note="")
            await db.set_setting("newsletter_last_sent", month_tag)
            log.info("monthly newsletter sent for %s", month_tag)
        except Exception:
            log.exception("scheduled newsletter failed")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("newsletter tick failed")
            await asyncio.sleep(300)


class AutoSyncScheduler:
    """Re-syncs media info for every library on the configured interval (0 = off)."""

    async def tick(self) -> None:
        interval_min = int(await db.get_setting("auto_sync_interval_min", 0))
        if interval_min <= 0:
            return
        last = int(await db.get_setting("auto_sync_last", 0))
        now = int(time.time())
        if now - last < interval_min * 60:
            return
        client = await plex.get_client()
        if not client.configured:
            return
        try:
            sections = await client.sections()
        except plex.PlexError:
            return
        await db.set_setting("auto_sync_last", now)
        for sec in sections:
            stype = sec.get("type", "")
            if stype in media_sync.LEAF_TYPE:
                await media_sync.sync_section(str(sec.get("key")), stype)
        log.info("auto-sync completed for all libraries")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("auto-sync tick failed")
            await asyncio.sleep(30)


activity_poller = ActivityPoller()
health_monitor = HealthMonitor()
newsletter_scheduler = NewsletterScheduler()
auto_sync_scheduler = AutoSyncScheduler()
