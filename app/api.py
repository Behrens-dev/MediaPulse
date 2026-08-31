"""All REST endpoints consumed by the web UI."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from . import db, plex, media_sync, notify
from .poller import activity_poller, health_monitor

router = APIRouter(prefix="/api")


def _err(e: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(e))


# ---------- status / settings ----------

@router.get("/status")
async def status():
    client = await plex.get_client()
    info = {}
    plex_ok, plex_error = False, ""
    if client.configured:
        try:
            mc = await client.server_info()
            info = {"name": mc.get("friendlyName"), "version": mc.get("version"),
                    "platform": mc.get("platform")}
            plex_ok = True
        except plex.PlexError as e:
            plex_error = str(e)
    return {
        "configured": client.configured,
        "plex_ok": plex_ok,
        "plex_error": plex_error,
        "server": info,
        "health": {
            "is_down": health_monitor.is_down,
            "last_ok_at": health_monitor.last_ok_at,
            "last_error": health_monitor.last_error,
        },
    }


SETTING_KEYS = [
    "plex_url", "plex_token", "server_display_name",
    "smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_security",
    "smtp_from", "smtp_from_name",
    "recipients", "alert_recipients", "alert_server_down",
    "newsletter_enabled", "newsletter_day", "newsletter_hour", "newsletter_days_back",
    "outage_auto_enabled", "outage_auto_delay_min", "auto_sync_interval_min",
]

DEFAULTS = {
    "smtp_port": 587, "smtp_security": "starttls", "smtp_from_name": "PlexPulse",
    "recipients": [], "alert_recipients": [], "alert_server_down": True,
    "newsletter_enabled": False, "newsletter_day": 1, "newsletter_hour": 9,
    "newsletter_days_back": 30,
    "outage_auto_enabled": False, "outage_auto_delay_min": 15,
    "auto_sync_interval_min": 0,
}


@router.get("/settings")
async def get_settings():
    out = {}
    for k in SETTING_KEYS:
        out[k] = await db.get_setting(k, DEFAULTS.get(k, ""))
    if not out["plex_url"]:
        client = await plex.get_client()  # picks up env-var fallback
        out["plex_url"], out["plex_token"] = client.base_url, client.token
    return out


@router.post("/settings")
async def save_settings(payload: dict):
    for k, v in payload.items():
        if k in SETTING_KEYS:
            await db.set_setting(k, v)
    return {"ok": True}


@router.post("/settings/test-plex")
async def test_plex(payload: dict):
    client = plex.PlexClient(payload.get("plex_url", ""), payload.get("plex_token", ""))
    try:
        mc = await client.server_info()
        return {"ok": True, "name": mc.get("friendlyName"), "version": mc.get("version")}
    except plex.PlexError as e:
        return {"ok": False, "error": str(e)}


@router.post("/settings/test-email")
async def test_email(payload: dict):
    to = payload.get("to")
    try:
        recips = await notify.send_test([to] if to else None)
        return {"ok": True, "recipients": recips}
    except notify.NotifyError as e:
        return {"ok": False, "error": str(e)}


# ---------- activity ----------

@router.get("/activity")
async def activity():
    return {
        "ok": activity_poller.last_poll_ok,
        "error": activity_poller.last_error,
        "sessions": activity_poller.last_sessions,
        "stream_count": len(activity_poller.last_sessions),
        "total_bandwidth_kbps": sum(s.get("bitrate_kbps") or 0 for s in activity_poller.last_sessions),
    }


# ---------- history ----------

@router.get("/history")
async def history(
    user: str = "",
    search: str = "",
    days: int = Query(0, ge=0),
    media_type: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    conn = await db.get_db()
    where, params = ["1=1"], []
    if user:
        where.append("user = ?"); params.append(user)
    if media_type:
        where.append("media_type = ?"); params.append(media_type)
    if search:
        where.append("(title LIKE ? OR grandparent_title LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if days:
        where.append("started_at >= ?"); params.append(int(time.time()) - days * 86400)
    w = " AND ".join(where)

    async with conn.execute(f"SELECT COUNT(*) c FROM history WHERE {w}", params) as cur:
        total = (await cur.fetchone())["c"]
    async with conn.execute(
        f"SELECT * FROM history WHERE {w} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    return {"total": total, "rows": rows}


@router.get("/history/users")
async def history_users():
    conn = await db.get_db()
    async with conn.execute(
        "SELECT user, COUNT(*) plays, MAX(started_at) last_play FROM history GROUP BY user ORDER BY plays DESC"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


@router.get("/history/stats")
async def history_stats(days: int = Query(30, ge=1)):
    conn = await db.get_db()
    since = int(time.time()) - days * 86400
    async with conn.execute(
        """SELECT COUNT(*) plays,
                  COUNT(DISTINCT user) users,
                  SUM(MAX(view_offset_ms, 0)) / 3600000.0 hours,
                  SUM(CASE WHEN stream_decision='transcode' THEN 1 ELSE 0 END) transcodes
           FROM history WHERE started_at >= ?""",
        (since,),
    ) as cur:
        row = dict(await cur.fetchone())
    row["hours"] = round(row["hours"] or 0, 1)
    return row


# ---------- libraries ----------

@router.get("/libraries")
async def libraries():
    client = await plex.get_client()
    try:
        sections = await client.sections()
    except plex.PlexError as e:
        raise _err(e)
    conn = await db.get_db()
    out = []
    for sec in sections:
        stype, key = sec.get("type"), str(sec.get("key"))
        counts = {}
        try:
            if stype == "movie":
                counts["movies"] = await client.section_count(key, plex.TYPE_MOVIE)
            elif stype == "show":
                counts["shows"] = await client.section_count(key, plex.TYPE_SHOW)
                counts["seasons"] = await client.section_count(key, plex.TYPE_SEASON)
                counts["episodes"] = await client.section_count(key, plex.TYPE_EPISODE)
            elif stype == "artist":
                counts["artists"] = await client.section_count(key, plex.TYPE_ARTIST)
                counts["albums"] = await client.section_count(key, plex.TYPE_ALBUM)
                counts["tracks"] = await client.section_count(key, plex.TYPE_TRACK)
            elif stype == "photo":
                counts["photos"] = await client.section_count(key, plex.TYPE_PHOTO)
                counts["videos"] = await client.section_count(key, plex.TYPE_CLIP)
        except plex.PlexError:
            pass

        # lifetime plays: Plex's own viewCount sum (from last sync) + our recorded history
        async with conn.execute(
            "SELECT COALESCE(SUM(view_count),0) vc, COUNT(*) items, COALESCE(SUM(file_size),0) size "
            "FROM media_items WHERE section_id=?", (key,)
        ) as cur:
            m = dict(await cur.fetchone())
        async with conn.execute(
            "SELECT COUNT(*) plays FROM history WHERE section_id=?", (key,)
        ) as cur:
            h = dict(await cur.fetchone())
        async with conn.execute(
            "SELECT state, total, done, error, updated_at FROM sync_status WHERE section_id=?", (key,)
        ) as cur:
            srow = await cur.fetchone()

        out.append({
            "key": key,
            "title": sec.get("title"),
            "type": stype,
            "counts": counts,
            "plex_view_count": m["vc"],
            "tracked_plays": h["plays"],
            "synced_items": m["items"],
            "synced_size_bytes": m["size"],
            "sync": dict(srow) if srow else None,
            "syncing": media_sync.is_running(key),
        })
    return out


@router.post("/libraries/{section_id}/sync")
async def start_sync(section_id: str):
    client = await plex.get_client()
    try:
        sections = await client.sections()
    except plex.PlexError as e:
        raise _err(e)
    sec = next((s for s in sections if str(s.get("key")) == section_id), None)
    if sec is None:
        raise HTTPException(404, "Library not found")
    if media_sync.is_running(section_id):
        return {"ok": True, "already_running": True}
    asyncio.get_running_loop().create_task(media_sync.sync_section(section_id, sec.get("type", "")))
    return {"ok": True}


@router.get("/libraries/{section_id}/sync-status")
async def sync_status(section_id: str):
    conn = await db.get_db()
    async with conn.execute("SELECT * FROM sync_status WHERE section_id=?", (section_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {"section_id": section_id, "state": "never"}


@router.get("/libraries/{section_id}/media")
async def library_media(
    section_id: str,
    search: str = "",
    container: str = "",
    resolution: str = "",
    video_codec: str = "",
    audio_codec: str = "",          # item must have at least one audio track with this codec
    audio_channels: str = "",       # e.g. "2" or "6" or "8"
    missing_stereo: bool = False,   # no 2.0 track at all
    only_surround: bool = False,    # has surround (>2ch) but no stereo
    sort: str = "title",
    order: str = "asc",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    conn = await db.get_db()
    where, params = ["m.section_id = ?"], [section_id]
    if search:
        where.append("(m.title LIKE ? OR m.grandparent_title LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if container:
        where.append("m.container = ?"); params.append(container)
    if resolution:
        where.append("m.video_resolution = ?"); params.append(resolution)
    if video_codec:
        where.append("m.video_codec = ?"); params.append(video_codec)
    if audio_codec:
        where.append("EXISTS (SELECT 1 FROM audio_streams a WHERE a.rating_key=m.rating_key AND a.codec=?)")
        params.append(audio_codec.lower())
    if audio_channels:
        where.append("EXISTS (SELECT 1 FROM audio_streams a WHERE a.rating_key=m.rating_key AND a.channels=?)")
        params.append(float(audio_channels))
    if missing_stereo or only_surround:
        where.append("NOT EXISTS (SELECT 1 FROM audio_streams a WHERE a.rating_key=m.rating_key AND a.channels<=2)")
    if only_surround:
        where.append("EXISTS (SELECT 1 FROM audio_streams a WHERE a.rating_key=m.rating_key AND a.channels>2)")
    w = " AND ".join(where)

    sort_col = {
        "title": "m.sort_title", "year": "m.year", "size": "m.file_size",
        "added": "m.added_at", "bitrate": "m.bitrate_kbps", "resolution": "m.video_resolution",
    }.get(sort, "m.sort_title")
    direction = "DESC" if order.lower() == "desc" else "ASC"

    async with conn.execute(f"SELECT COUNT(*) c FROM media_items m WHERE {w}", params) as cur:
        total = (await cur.fetchone())["c"]
    async with conn.execute(
        f"SELECT m.* FROM media_items m WHERE {w} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    # attach the full audio track list for the page of items
    keys = [r["rating_key"] for r in rows]
    tracks: dict[str, list] = {k: [] for k in keys}
    if keys:
        ph = ",".join("?" * len(keys))
        async with conn.execute(
            f"SELECT * FROM audio_streams WHERE rating_key IN ({ph}) ORDER BY rating_key, stream_index",
            keys,
        ) as cur:
            for t in await cur.fetchall():
                tracks[t["rating_key"]].append(dict(t))
    for r in rows:
        r["audio_tracks"] = tracks.get(r["rating_key"], [])
    return {"total": total, "rows": rows}


@router.get("/libraries/{section_id}/audio-summary")
async def audio_summary(section_id: str):
    """Aggregate view of the audio landscape of a library — the 'what am I missing' report."""
    conn = await db.get_db()

    async with conn.execute(
        "SELECT COUNT(*) c FROM media_items WHERE section_id=?", (section_id,)
    ) as cur:
        total = (await cur.fetchone())["c"]

    async with conn.execute(
        """SELECT a.codec, COUNT(DISTINCT a.rating_key) items
           FROM audio_streams a JOIN media_items m ON m.rating_key=a.rating_key
           WHERE m.section_id=? GROUP BY a.codec ORDER BY items DESC""",
        (section_id,),
    ) as cur:
        by_codec = [dict(r) for r in await cur.fetchall()]

    async with conn.execute(
        """SELECT a.channels, COUNT(DISTINCT a.rating_key) items
           FROM audio_streams a JOIN media_items m ON m.rating_key=a.rating_key
           WHERE m.section_id=? GROUP BY a.channels ORDER BY a.channels DESC""",
        (section_id,),
    ) as cur:
        by_channels = [dict(r) for r in await cur.fetchall()]

    async with conn.execute(
        """SELECT COUNT(*) c FROM media_items m WHERE m.section_id=? AND NOT EXISTS
           (SELECT 1 FROM audio_streams a WHERE a.rating_key=m.rating_key AND a.channels<=2)""",
        (section_id,),
    ) as cur:
        missing_stereo = (await cur.fetchone())["c"]

    async with conn.execute(
        """SELECT COUNT(*) c FROM media_items m WHERE m.section_id=? AND
           (SELECT COUNT(*) FROM audio_streams a WHERE a.rating_key=m.rating_key) > 1""",
        (section_id,),
    ) as cur:
        multi_track = (await cur.fetchone())["c"]

    async with conn.execute(
        "SELECT container, COUNT(*) items FROM media_items WHERE section_id=? GROUP BY container ORDER BY items DESC",
        (section_id,),
    ) as cur:
        by_container = [dict(r) for r in await cur.fetchall()]

    async with conn.execute(
        "SELECT video_resolution, COUNT(*) items FROM media_items WHERE section_id=? GROUP BY video_resolution ORDER BY items DESC",
        (section_id,),
    ) as cur:
        by_resolution = [dict(r) for r in await cur.fetchall()]

    return {
        "total_items": total,
        "missing_stereo": missing_stereo,
        "multi_track": multi_track,
        "by_codec": by_codec,
        "by_channels": by_channels,
        "by_container": by_container,
        "by_resolution": by_resolution,
    }


# ---------- search / recently added (for notification builders) ----------

@router.get("/search")
async def search(q: str):
    client = await plex.get_client()
    try:
        results = await client.search(q)
    except plex.PlexError as e:
        raise _err(e)
    return [
        {
            "rating_key": m.get("ratingKey"),
            "title": m.get("title"),
            "grandparent_title": m.get("grandparentTitle", ""),
            "parent_title": m.get("parentTitle", ""),
            "year": m.get("year"),
            "type": m.get("type"),
            "summary": (m.get("summary") or "")[:300],
            "thumb": m.get("thumb", ""),
        }
        for m in results
    ]


@router.get("/recently-added")
async def recently_added(days: int = Query(30, ge=1, le=365)):
    client = await plex.get_client()
    cutoff = int(time.time()) - days * 86400
    try:
        sections = await client.sections()
        out = []
        for sec in sections:
            if sec.get("type") not in ("movie", "show", "artist"):
                continue
            items = await client.recently_added(sec["key"], limit=100)
            for m in items:
                if int(m.get("addedAt") or 0) >= cutoff:
                    out.append({
                        "library": sec.get("title"),
                        "title": m.get("title"),
                        "grandparent_title": m.get("grandparentTitle", ""),
                        "year": m.get("year"),
                        "type": m.get("type"),
                        "added_at": m.get("addedAt"),
                    })
        out.sort(key=lambda x: x["added_at"] or 0, reverse=True)
        return out
    except plex.PlexError as e:
        raise _err(e)


# ---------- notifications ----------

class NewsletterReq(BaseModel):
    days_back: int = 30
    note: str = ""
    recipients: list[str] | None = None


class RecItem(BaseModel):
    title: str
    year: int | str | None = None
    type: str = ""
    note: str = ""
    summary: str = ""
    thumb: str = ""


class RecommendReq(BaseModel):
    heading: str = ""
    intro: str = ""
    items: list[RecItem]
    recipients: list[str] | None = None


class MaintenanceReq(BaseModel):
    start: str
    end: str
    message: str = ""
    image_b64: str = ""
    image_mime: str = ""
    recipients: list[str] | None = None


class OutageReq(BaseModel):
    message: str = ""
    eta: str = ""
    image_b64: str = ""
    image_mime: str = ""
    recipients: list[str] | None = None


@router.post("/notify/newsletter/preview")
async def newsletter_preview(req: NewsletterReq):
    try:
        subject, html, images = await notify.build_recently_added(req.days_back, req.note)
    except (plex.PlexError, notify.NotifyError) as e:
        raise _err(e)
    return {"subject": subject, "html": notify.inline_images(html, images)}


@router.post("/notify/newsletter/send")
async def newsletter_send(req: NewsletterReq):
    try:
        recips = await notify.send_recently_added(req.days_back, req.note, req.recipients)
        return {"ok": True, "recipients": recips}
    except (plex.PlexError, notify.NotifyError) as e:
        return {"ok": False, "error": str(e)}


@router.post("/notify/recommend/preview")
async def recommend_preview(req: RecommendReq):
    items = [i.model_dump() for i in req.items]
    subject, html, images = await notify.build_recommendations(items, req.intro, req.heading)
    return {"subject": subject, "html": notify.inline_images(html, images)}


@router.post("/notify/recommend/send")
async def recommend_send(req: RecommendReq):
    items = [i.model_dump() for i in req.items]
    try:
        recips = await notify.send_recommendations(items, req.intro, req.heading, req.recipients)
        return {"ok": True, "recipients": recips}
    except notify.NotifyError as e:
        return {"ok": False, "error": str(e)}


@router.post("/notify/maintenance/preview")
async def maintenance_preview(req: MaintenanceReq):
    try:
        subject, html, images = await notify.build_maintenance(
            req.start, req.end, req.message, req.image_b64, req.image_mime)
    except notify.NotifyError as e:
        raise _err(e)
    return {"subject": subject, "html": notify.inline_images(html, images)}


@router.post("/notify/maintenance/send")
async def maintenance_send(req: MaintenanceReq):
    try:
        recips = await notify.send_maintenance(
            req.start, req.end, req.message, req.image_b64, req.image_mime, req.recipients)
        return {"ok": True, "recipients": recips}
    except notify.NotifyError as e:
        return {"ok": False, "error": str(e)}


@router.post("/notify/outage/preview")
async def outage_preview(req: OutageReq):
    try:
        subject, html, images = await notify.build_outage(
            req.message, req.eta, req.image_b64, req.image_mime)
    except notify.NotifyError as e:
        raise _err(e)
    return {"subject": subject, "html": notify.inline_images(html, images)}


@router.post("/notify/outage/send")
async def outage_send(req: OutageReq):
    try:
        recips = await notify.send_outage(
            req.message, req.eta, req.image_b64, req.image_mime, recipients=req.recipients)
        return {"ok": True, "recipients": recips}
    except notify.NotifyError as e:
        return {"ok": False, "error": str(e)}


@router.get("/notify/log")
async def notify_log(limit: int = Query(50, le=200)):
    conn = await db.get_db()
    async with conn.execute(
        "SELECT * FROM sent_log ORDER BY sent_at DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ---------- users ----------

@router.get("/users")
async def users():
    client = await plex.get_client()
    aliases = await db.get_setting("user_aliases", {})
    try:
        machine_id = (await client.server_info()).get("machineIdentifier", "")
        shared = await client.plextv_users()
    except plex.PlexError as e:
        raise _err(e)

    # per-user play stats from our own history (matched by Plex username/title)
    conn = await db.get_db()
    async with conn.execute(
        "SELECT user, COUNT(*) plays, MAX(started_at) last_play FROM history GROUP BY user"
    ) as cur:
        stats = {r["user"]: dict(r) for r in await cur.fetchall()}

    out = []

    # server owner first
    try:
        acct = await client.plextv_account()
        owner_name = acct.get("username") or acct.get("title") or "Owner"
        st = stats.get(owner_name) or stats.get(acct.get("title", ""), {})
        out.append({
            "id": str(acct.get("id", "owner")),
            "username": owner_name,
            "email": acct.get("email", ""),
            "alias": aliases.get(str(acct.get("id", "owner")), ""),
            "access": "Owner — all libraries",
            "home": True,
            "restricted": False,
            "plays": st.get("plays", 0),
            "last_play": st.get("last_play"),
        })
    except plex.PlexError:
        pass

    for u in shared:
        srv = next((s for s in u["servers"] if s["machine_id"] == machine_id), None)
        if machine_id and srv is None and u["servers"]:
            continue  # shares a different server, not this one
        if srv and srv.get("pending"):
            access = "Invite pending"
        elif srv is None:
            access = "Plex Home" if u["home"] else "Shared"
        elif srv["all_libraries"]:
            access = "All libraries"
        else:
            access = f"{srv['num_libraries']} librar{'y' if srv['num_libraries'] == 1 else 'ies'}"
        if u["home"]:
            access += " · Home user"
        if u["restricted"]:
            access += " · Managed/restricted"
        display = u["username"] or u["title"]
        st = stats.get(display) or stats.get(u["title"], {})
        out.append({
            "id": str(u["id"]),
            "username": display,
            "email": u["email"],
            "alias": aliases.get(str(u["id"]), ""),
            "access": access,
            "home": u["home"],
            "restricted": u["restricted"],
            "plays": st.get("plays", 0),
            "last_play": st.get("last_play"),
        })
    return out


@router.post("/users/alias")
async def set_alias(payload: dict):
    user_id = str(payload.get("user_id", ""))
    alias = (payload.get("alias") or "").strip()
    if not user_id:
        raise HTTPException(400, "user_id required")
    aliases = await db.get_setting("user_aliases", {})
    if alias:
        aliases[user_id] = alias
    else:
        aliases.pop(user_id, None)
    await db.set_setting("user_aliases", aliases)
    return {"ok": True}


# ---------- image proxy (posters in the UI without exposing the token) ----------

@router.get("/pximg")
async def plex_image(path: str, w: int = 240, h: int = 360):
    if not path.startswith("/"):
        raise HTTPException(400, "bad path")
    client = await plex.get_client()
    try:
        body, ctype = await client.get_bytes(
            "/photo/:/transcode",
            params={"url": path, "width": w, "height": h, "minSize": 1},
        )
    except plex.PlexError:
        raise HTTPException(404, "image unavailable")
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})
