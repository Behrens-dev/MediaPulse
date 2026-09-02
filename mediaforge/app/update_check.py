"""Weekly update check: every Sunday at midnight (container time), compare the
commit SHA this image was built from against the repo's current main branch.
Purely informational â€” nothing is ever installed automatically; updating is
still a manual stop/start of the app (with pull_policy: always in the YAML)."""
import asyncio
import datetime
import logging
import time

import httpx

from . import db
from .config import GIT_SHA, UPDATE_REPO

log = logging.getLogger("mediaforge.update")

STALE_SECONDS = 8 * 24 * 3600   # catch-up threshold after downtime


async def check_now() -> dict:
    info = {"checked_at": int(time.time()), "latest_sha": "", "available": False, "error": ""}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.github.com/repos/{UPDATE_REPO}/commits/main",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "mediaforge-update-check"},
                timeout=20.0,
            )
        if r.status_code == 200:
            sha = (r.json() or {}).get("sha") or ""
            info["latest_sha"] = sha
            info["available"] = bool(GIT_SHA and sha and sha != GIT_SHA)
        else:
            info["error"] = f"GitHub returned HTTP {r.status_code}"
    except Exception as e:
        info["error"] = str(e)
    await db.set_setting("update_status", info)
    if info["available"]:
        log.info("update available: running %s, latest %s", GIT_SHA[:7], info["latest_sha"][:7])
    return info


def _seconds_until_sunday_midnight() -> float:
    now = datetime.datetime.now()
    days = (6 - now.weekday()) % 7  # Monday=0 â€¦ Sunday=6
    target = (now + datetime.timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=7)
    return (target - now).total_seconds()


async def run() -> None:
    if not GIT_SHA:
        log.info("no GIT_SHA baked into this build â€” update checks disabled")
        return
    while True:
        try:
            if await db.get_setting("update_check_enabled", True):
                st = await db.get_setting("update_status", {}) or {}
                if time.time() - int(st.get("checked_at") or 0) > STALE_SECONDS:
                    await check_now()  # first run / catching up after downtime
            wait = _seconds_until_sunday_midnight()
            if wait <= 6 * 3600:
                await asyncio.sleep(wait + 5)
                if await db.get_setting("update_check_enabled", True):
                    await check_now()
            else:
                await asyncio.sleep(6 * 3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("update check loop error")
            await asyncio.sleep(3600)

