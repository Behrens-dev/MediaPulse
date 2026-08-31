"""Email notifications: SMTP delivery + the template library
(recently added, recommendations, maintenance window, outage alerts, server up/down).

Every build_* function returns (subject, html, images) where `images` is a list of
{"cid", "data", "mime"} dicts. When sending, images ride along as inline CID
attachments; for browser previews they are swapped in as data: URIs."""
import asyncio
import base64
import logging
import smtplib
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, plex

log = logging.getLogger("plexpulse.notify")

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)

MAX_POSTERS = 40          # cap inline images per email
MAX_IMAGE_BYTES = 6_000_000


class NotifyError(Exception):
    pass


# ---------- SMTP plumbing ----------

async def _smtp_settings() -> dict:
    s = {
        "host": await db.get_setting("smtp_host", ""),
        "port": int(await db.get_setting("smtp_port", 587)),
        "username": await db.get_setting("smtp_username", ""),
        "password": await db.get_setting("smtp_password", ""),
        "security": await db.get_setting("smtp_security", "starttls"),  # starttls | ssl | none
        "from_addr": await db.get_setting("smtp_from", ""),
        "from_name": await db.get_setting("smtp_from_name", "PlexPulse"),
    }
    if not s["host"] or not s["from_addr"]:
        raise NotifyError("SMTP is not configured yet — set it up under Notifications → Email settings.")
    return s


async def _recipients(override: list[str] | None = None) -> list[str]:
    if override:
        return override
    recips = await db.get_setting("recipients", [])
    if not recips:
        raise NotifyError("No recipients configured — add at least one email under Notifications.")
    return recips


def _send_sync(smtp: dict, subject: str, html: str, recipients: list[str],
               images: list[dict]) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = f"{smtp['from_name']} <{smtp['from_addr']}>"
    # recipients are BCC'd: only the sender shows in To, the real recipient list
    # rides on the SMTP envelope so people can't see each other's addresses
    msg["To"] = f"{smtp['from_name']} <{smtp['from_addr']}>"

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("This message contains HTML content.", "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    for img in images:
        subtype = (img.get("mime") or "image/jpeg").split("/")[-1]
        part = MIMEImage(img["data"], _subtype=subtype)
        part.add_header("Content-ID", f"<{img['cid']}>")
        part.add_header("Content-Disposition", "inline", filename=f"{img['cid']}.{subtype}")
        msg.attach(part)

    if smtp["security"] == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30)
    else:
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=30)
    try:
        if smtp["security"] == "starttls":
            server.starttls()
        if smtp["username"]:
            server.login(smtp["username"], smtp["password"])
        server.sendmail(smtp["from_addr"], recipients, msg.as_string())
    finally:
        server.quit()


async def send_email(kind: str, subject: str, html: str,
                     recipients: list[str] | None = None,
                     images: list[dict] | None = None) -> list[str]:
    smtp = await _smtp_settings()
    recips = await _recipients(recipients)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _send_sync, smtp, subject, html, recips, images or [])
        await db.log_sent(kind, subject, recips, ok=True, sent_at=int(time.time()))
        return recips
    except NotifyError:
        raise
    except Exception as e:
        await db.log_sent(kind, subject, recips, ok=False, error=str(e), sent_at=int(time.time()))
        raise NotifyError(f"Email send failed: {e}") from e


def inline_images(html: str, images: list[dict]) -> str:
    """Swap cid: references for data: URIs so the browser preview shows the images."""
    for img in images:
        b64 = base64.b64encode(img["data"]).decode()
        html = html.replace(f"cid:{img['cid']}", f"data:{img.get('mime') or 'image/jpeg'};base64,{b64}")
    return html


def decode_upload(image_b64: str, image_mime: str) -> list[dict]:
    """Turn a user-uploaded base64 image (maintenance/outage) into an images list."""
    if not image_b64:
        return []
    try:
        data = base64.b64decode(image_b64)
    except Exception as e:
        raise NotifyError("Could not decode the uploaded image") from e
    if len(data) > MAX_IMAGE_BYTES:
        raise NotifyError("Image is too large — keep it under 6 MB.")
    return [{"cid": "custom0", "data": data, "mime": image_mime or "image/jpeg"}]


async def _server_name() -> str:
    name = await db.get_setting("server_display_name", "")
    if name:
        return name
    try:
        client = await plex.get_client()
        info = await client.server_info()
        return info.get("friendlyName", "Plex Server")
    except Exception:
        return "Plex Server"


class _PosterCollector:
    """Deduplicating poster fetcher; silently skips failures and respects the cap."""

    def __init__(self, client: plex.PlexClient):
        self.client = client
        self.images: list[dict] = []
        self._seen: dict[str, str | None] = {}

    async def cid_for(self, thumb: str | None) -> str | None:
        if not thumb or len(self.images) >= MAX_POSTERS:
            return None
        if thumb in self._seen:
            return self._seen[thumb]
        try:
            data, mime = await self.client.poster(thumb)
        except Exception:
            self._seen[thumb] = None
            return None
        cid = f"poster{len(self.images)}"
        self.images.append({"cid": cid, "data": data, "mime": mime})
        self._seen[thumb] = cid
        return cid


# ---------- Recently added ----------

async def build_recently_added(days_back: int = 30, note: str = "") -> tuple[str, str, list[dict]]:
    client = await plex.get_client()
    cutoff = int(time.time()) - days_back * 86400
    sections = await client.sections()
    posters = _PosterCollector(client)
    groups = []

    for sec in sections:
        stype, key = sec.get("type"), str(sec.get("key"))
        entries: list[dict] = []

        if stype == "movie":
            for m in (await client.added_since(key, plex.TYPE_MOVIE, cutoff))[:40]:
                entries.append({
                    "title": m.get("title", ""),
                    "year": m.get("year"),
                    "rating": m.get("contentRating") or "",
                    "summary": (m.get("summary") or "")[:350],
                    "cid": await posters.cid_for(m.get("thumb")),
                    "bullets": [],
                })

        elif stype == "show":
            # one entry per show: series poster + summary, then a bullet per new episode
            eps = await client.added_since(key, plex.TYPE_EPISODE, cutoff)
            shows: dict[str, dict] = {}
            for e in eps:
                gp = str(e.get("grandparentRatingKey") or e.get("grandparentTitle") or "?")
                s = shows.setdefault(gp, {
                    "title": e.get("grandparentTitle", ""),
                    "thumb": e.get("grandparentThumb"),
                    "eps": [],
                })
                s["eps"].append((int(e.get("parentIndex") or 0),
                                 int(e.get("index") or 0),
                                 e.get("title", "")))
            gp_keys = [k for k in shows if k.isdigit()]
            meta: dict[str, dict] = {}
            for i in range(0, len(gp_keys), 25):
                try:
                    for m in await client.metadata_batch(gp_keys[i:i + 25]):
                        meta[str(m.get("ratingKey"))] = m
                except plex.PlexError:
                    pass
            for gp, s in shows.items():
                info = meta.get(gp, {})
                eps_sorted = sorted(s["eps"])
                bullets = [f"S{se:02d}E{ep:02d} · {t}" for se, ep, t in eps_sorted[:30]]
                if len(eps_sorted) > 30:
                    bullets.append(f"…and {len(eps_sorted) - 30} more episodes")
                entries.append({
                    "title": s["title"],
                    "year": info.get("year"),
                    "rating": info.get("contentRating") or "",
                    "summary": (info.get("summary") or "")[:350],
                    "cid": await posters.cid_for(info.get("thumb") or s["thumb"]),
                    "bullets": bullets,
                })

        elif stype == "artist":
            # one entry per album with its new tracks
            tracks = await client.added_since(key, plex.TYPE_TRACK, cutoff)
            albums: dict[str, dict] = {}
            for t in tracks:
                akey = str(t.get("parentRatingKey") or f"{t.get('grandparentTitle')}|{t.get('parentTitle')}")
                a = albums.setdefault(akey, {
                    "artist": t.get("grandparentTitle", ""),
                    "album": t.get("parentTitle", ""),
                    "thumb": t.get("parentThumb") or t.get("grandparentThumb"),
                    "year": t.get("parentYear"),
                    "tracks": [],
                })
                a["tracks"].append((int(t.get("index") or 0), t.get("title", "")))
            for a in albums.values():
                ts = sorted(a["tracks"])
                bullets = [title for _, title in ts[:20]]
                if len(ts) > 20:
                    bullets.append(f"…and {len(ts) - 20} more tracks")
                entries.append({
                    "title": f"{a['artist']} — {a['album']}",
                    "year": a.get("year"),
                    "rating": "",
                    "summary": "",
                    "cid": await posters.cid_for(a["thumb"]),
                    "bullets": bullets,
                })
        else:
            continue

        if entries:
            groups.append({"library": sec.get("title", ""), "entries": entries})

    server = await _server_name()
    subject = f"{server}: Recently added (last {days_back} days)"
    html = _env.get_template("newsletter.html").render(
        server=server, days_back=days_back, groups=groups, note=note,
        month=time.strftime("%B %Y"),
    )
    return subject, html, posters.images


async def send_recently_added(days_back: int = 30, note: str = "",
                              recipients: list[str] | None = None) -> list[str]:
    subject, html, images = await build_recently_added(days_back, note)
    return await send_email("recently_added", subject, html, recipients, images)


# backwards-compatible aliases (scheduler predates the rename)
build_newsletter = build_recently_added
send_newsletter = send_recently_added


# ---------- Recommendations ----------

async def build_recommendations(items: list[dict], intro: str = "",
                                heading: str = "") -> tuple[str, str, list[dict]]:
    client = await plex.get_client()
    posters = _PosterCollector(client)
    for item in items:
        item["cid"] = await posters.cid_for(item.get("thumb"))
    server = await _server_name()
    heading = heading or "My recommended watch list"
    subject = f"{server}: {heading}"
    html = _env.get_template("recommendations.html").render(
        server=server, heading=heading, intro=intro, items=items,
    )
    return subject, html, posters.images


async def send_recommendations(items: list[dict], intro: str = "", heading: str = "",
                               recipients: list[str] | None = None) -> list[str]:
    subject, html, images = await build_recommendations(items, intro, heading)
    return await send_email("recommendations", subject, html, recipients, images)


# ---------- Maintenance window ----------

async def build_maintenance(start: str, end: str, message: str = "",
                            image_b64: str = "", image_mime: str = "") -> tuple[str, str, list[dict]]:
    images = decode_upload(image_b64, image_mime)
    server = await _server_name()
    subject = f"{server}: Scheduled maintenance"
    html = _env.get_template("maintenance.html").render(
        server=server, start=start, end=end, message=message,
        image_cid=images[0]["cid"] if images else None,
    )
    return subject, html, images


async def send_maintenance(start: str, end: str, message: str = "",
                           image_b64: str = "", image_mime: str = "",
                           recipients: list[str] | None = None) -> list[str]:
    subject, html, images = await build_maintenance(start, end, message, image_b64, image_mime)
    return await send_email("maintenance", subject, html, recipients, images)


# ---------- Outage alerts ----------

async def build_outage(message: str = "", eta: str = "",
                       image_b64: str = "", image_mime: str = "",
                       auto: bool = False) -> tuple[str, str, list[dict]]:
    images = decode_upload(image_b64, image_mime)
    server = await _server_name()
    subject = f"{server}: Service outage"
    html = _env.get_template("outage.html").render(
        server=server, message=message, eta=eta, auto=auto,
        when=time.strftime("%Y-%m-%d %H:%M:%S"),
        image_cid=images[0]["cid"] if images else None,
    )
    return subject, html, images


async def send_outage(message: str = "", eta: str = "",
                      image_b64: str = "", image_mime: str = "",
                      auto: bool = False,
                      recipients: list[str] | None = None) -> list[str]:
    subject, html, images = await build_outage(message, eta, image_b64, image_mime, auto)
    kind = "outage_auto" if auto else "outage"
    return await send_email(kind, subject, html, recipients, images)


# ---------- Server up/down (admin alerts) ----------

async def send_server_status(down: bool, error: str = "") -> None:
    server = await _server_name()
    # down alerts go to the admin list if set, otherwise the main recipient list
    admin = await db.get_setting("alert_recipients", [])
    subject = f"{server} is DOWN" if down else f"{server} is back online"
    html = _env.get_template("server_status.html").render(
        server=server, down=down, error=error,
        when=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        await send_email("server_status", subject, html, admin or None)
    except NotifyError as e:
        log.warning("could not send server status alert: %s", e)


async def send_test(recipients: list[str] | None = None) -> list[str]:
    server = await _server_name()
    html = _env.get_template("server_status.html").render(
        server=server, down=False, error="", test=True,
        when=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return await send_email("test", f"PlexPulse test email ({server})", html, recipients)
