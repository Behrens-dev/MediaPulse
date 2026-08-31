"""Email notifications: SMTP delivery + the template library
(newsletter, recommendations, maintenance window, server up/down)."""
import asyncio
import logging
import smtplib
import time
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


class NotifyError(Exception):
    pass


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


def _send_sync(smtp: dict, subject: str, html: str, recipients: list[str]) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{smtp['from_name']} <{smtp['from_addr']}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("This message contains HTML content.", "plain"))
    msg.attach(MIMEText(html, "html"))

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


async def send_email(kind: str, subject: str, html: str, recipients: list[str] | None = None) -> list[str]:
    smtp = await _smtp_settings()
    recips = await _recipients(recipients)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _send_sync, smtp, subject, html, recips)
        await db.log_sent(kind, subject, recips, ok=True, sent_at=int(time.time()))
        return recips
    except Exception as e:
        await db.log_sent(kind, subject, recips, ok=False, error=str(e), sent_at=int(time.time()))
        raise NotifyError(f"Email send failed: {e}") from e


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


def _fmt_item(m: dict) -> dict:
    """Normalize a Plex metadata object for email templates."""
    t = m.get("type")
    if t == "episode":
        title = f"{m.get('grandparentTitle', '')} — S{m.get('parentIndex', 0):02d}E{m.get('index', 0):02d} · {m.get('title', '')}"
    elif t == "season":
        title = f"{m.get('parentTitle', '')} — {m.get('title', '')}"
    elif t == "album":
        title = f"{m.get('parentTitle', '')} — {m.get('title', '')}"
    else:
        title = m.get("title", "")
    return {
        "title": title,
        "year": m.get("year") or "",
        "type": t or "",
        "summary": (m.get("summary") or "")[:400],
        "rating": m.get("contentRating") or "",
        "added_at": m.get("addedAt"),
    }


# ---- Newsletter (recently added) ----

async def build_newsletter(days_back: int = 30, note: str = "") -> tuple[str, str]:
    client = await plex.get_client()
    cutoff = int(time.time()) - days_back * 86400
    sections = await client.sections()
    groups = []
    for sec in sections:
        if sec.get("type") not in ("movie", "show", "artist"):
            continue
        items = await client.recently_added(sec["key"], limit=200)
        fresh = [_fmt_item(m) for m in items if int(m.get("addedAt") or 0) >= cutoff]
        # collapse episodes into one line per show where possible
        if fresh:
            groups.append({"library": sec.get("title", ""), "entries": fresh[:60]})

    server = await _server_name()
    subject = f"{server}: What's new (last {days_back} days)"
    html = _env.get_template("newsletter.html").render(
        server=server, days_back=days_back, groups=groups, note=note,
        month=time.strftime("%B %Y"),
    )
    return subject, html


async def send_newsletter(days_back: int = 30, note: str = "", recipients: list[str] | None = None) -> list[str]:
    subject, html = await build_newsletter(days_back, note)
    return await send_email("newsletter", subject, html, recipients)


# ---- Recommendations ----

async def build_recommendations(items: list[dict], intro: str = "", heading: str = "") -> tuple[str, str]:
    server = await _server_name()
    heading = heading or "My recommended watch list"
    subject = f"{server}: {heading}"
    html = _env.get_template("recommendations.html").render(
        server=server, heading=heading, intro=intro, items=items,
    )
    return subject, html


async def send_recommendations(items: list[dict], intro: str = "", heading: str = "",
                               recipients: list[str] | None = None) -> list[str]:
    subject, html = await build_recommendations(items, intro, heading)
    return await send_email("recommendations", subject, html, recipients)


# ---- Maintenance window ----

async def build_maintenance(start: str, end: str, message: str = "") -> tuple[str, str]:
    server = await _server_name()
    subject = f"{server}: Scheduled maintenance"
    html = _env.get_template("maintenance.html").render(
        server=server, start=start, end=end, message=message,
    )
    return subject, html


async def send_maintenance(start: str, end: str, message: str = "",
                           recipients: list[str] | None = None) -> list[str]:
    subject, html = await build_maintenance(start, end, message)
    return await send_email("maintenance", subject, html, recipients)


# ---- Server up/down ----

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
