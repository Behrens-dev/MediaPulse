"""Thin async client for the Plex Media Server HTTP API (JSON responses)."""
import xml.etree.ElementTree as ET

import httpx

from . import db
from .config import ENV_PLEX_URL, ENV_PLEX_TOKEN

# Plex library item types
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_SEASON = 3
TYPE_EPISODE = 4
TYPE_ARTIST = 8
TYPE_ALBUM = 9
TYPE_TRACK = 10
TYPE_CLIP = 12
TYPE_PHOTO = 13

PLEXTV = "https://plex.tv"


class PlexError(Exception):
    pass


class PlexClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict:
        return {
            "X-Plex-Token": self.token,
            "Accept": "application/json",
            "X-Plex-Client-Identifier": "mediapulse",
            "X-Plex-Product": "MediaPulse",
            "X-Plex-Version": "1.0",
        }

    async def get(self, path: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if not self.configured:
            raise PlexError("Plex URL / token not configured. Set them in Settings.")
        async with httpx.AsyncClient(verify=False) as client:
            try:
                r = await client.get(
                    f"{self.base_url}{path}",
                    params=params or {},
                    headers=self._headers(),
                    timeout=timeout,
                )
            except httpx.HTTPError as e:
                raise PlexError(f"Cannot reach Plex at {self.base_url}: {e}") from e
        if r.status_code == 401:
            raise PlexError("Plex rejected the token (401 Unauthorized).")
        if r.status_code >= 400:
            raise PlexError(f"Plex returned HTTP {r.status_code} for {path}")
        try:
            return r.json().get("MediaContainer", {})
        except ValueError as e:
            raise PlexError(f"Plex returned a non-JSON response for {path}") from e

    async def get_bytes(self, path: str, params: dict | None = None) -> tuple[bytes, str]:
        """Fetch raw bytes (poster images, etc.). Returns (body, content_type)."""
        if not self.configured:
            raise PlexError("Plex not configured")
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(
                f"{self.base_url}{path}",
                params=params or {},
                headers={"X-Plex-Token": self.token},
                timeout=30.0,
            )
        if r.status_code >= 400:
            raise PlexError(f"HTTP {r.status_code}")
        return r.content, r.headers.get("content-type", "image/jpeg")

    # ---- endpoints ----

    async def server_info(self) -> dict:
        return await self.get("/", timeout=10.0)

    async def sessions(self) -> list[dict]:
        mc = await self.get("/status/sessions", timeout=15.0)
        return mc.get("Metadata", [])

    async def sections(self) -> list[dict]:
        mc = await self.get("/library/sections")
        return mc.get("Directory", [])

    async def section_count(self, section_key: str, item_type: int) -> int:
        mc = await self.get(
            f"/library/sections/{section_key}/all",
            params={"type": item_type, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
        )
        return int(mc.get("totalSize", 0))

    async def section_items(self, section_key: str, item_type: int, start: int, size: int) -> dict:
        return await self.get(
            f"/library/sections/{section_key}/all",
            params={
                "type": item_type,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": size,
            },
            timeout=120.0,
        )

    async def metadata_batch(self, rating_keys: list[str]) -> list[dict]:
        """Full metadata (incl. per-file Stream elements) for up to ~25 items at once."""
        if not rating_keys:
            return []
        mc = await self.get(f"/library/metadata/{','.join(rating_keys)}", timeout=120.0)
        return mc.get("Metadata", [])

    async def recently_added(self, section_key: str | None, limit: int = 200) -> list[dict]:
        if section_key:
            mc = await self.get(
                f"/library/sections/{section_key}/recentlyAdded",
                params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit},
            )
        else:
            mc = await self.get(
                "/library/recentlyAdded",
                params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit},
            )
        return mc.get("Metadata", [])

    async def added_since(self, section_key: str, item_type: int, cutoff: int,
                          max_items: int = 600) -> list[dict]:
        """All leaf items in a section added after `cutoff` (newest first)."""
        items: list[dict] = []
        start = 0
        while start < max_items:
            mc = await self.get(
                f"/library/sections/{section_key}/all",
                params={
                    "type": item_type,
                    "sort": "addedAt:desc",
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": 100,
                },
                timeout=60.0,
            )
            batch = mc.get("Metadata", [])
            if not batch:
                break
            for m in batch:
                if int(m.get("addedAt") or 0) >= cutoff:
                    items.append(m)
                else:
                    return items
            start += len(batch)
            if start >= int(mc.get("totalSize", 0)):
                break
        return items

    async def poster(self, thumb_path: str, width: int = 240, height: int = 360) -> tuple[bytes, str]:
        return await self.get_bytes(
            "/photo/:/transcode",
            params={"url": thumb_path, "width": width, "height": height, "minSize": 1},
        )

    # ---- plex.tv (account / shared users) ----

    async def plextv_users(self) -> list[dict]:
        """Users this account shares servers with (incl. Plex Home users), from plex.tv."""
        if not self.token:
            raise PlexError("Plex token not configured")
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(
                    f"{PLEXTV}/api/users",
                    headers={"X-Plex-Token": self.token,
                             "X-Plex-Client-Identifier": "mediapulse",
                             "X-Plex-Product": "MediaPulse"},
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                raise PlexError(f"Cannot reach plex.tv: {e}") from e
        if r.status_code == 401:
            raise PlexError("plex.tv rejected the token (401). The Users page needs your "
                            "account owner token.")
        if r.status_code >= 400:
            raise PlexError(f"plex.tv returned HTTP {r.status_code}")
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            raise PlexError("plex.tv returned an unexpected response") from e
        users = []
        for u in root.findall("User"):
            users.append({
                "id": u.get("id"),
                "username": u.get("username") or u.get("title") or "",
                "title": u.get("title") or "",
                "email": u.get("email") or "",
                "thumb": u.get("thumb") or "",
                "home": u.get("home") == "1",
                "restricted": u.get("restricted") == "1",
                "servers": [
                    {
                        "machine_id": s.get("machineIdentifier"),
                        "all_libraries": s.get("allLibraries") == "1",
                        "num_libraries": int(s.get("numLibraries") or 0),
                        "pending": s.get("pending") == "1",
                    }
                    for s in u.findall("Server")
                ],
            })
        return users

    async def plextv_account(self) -> dict:
        """The server owner's own plex.tv account."""
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(
                    f"{PLEXTV}/api/v2/user",
                    headers={"X-Plex-Token": self.token, "Accept": "application/json",
                             "X-Plex-Client-Identifier": "mediapulse",
                             "X-Plex-Product": "MediaPulse"},
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                raise PlexError(f"Cannot reach plex.tv: {e}") from e
        if r.status_code >= 400:
            raise PlexError(f"plex.tv returned HTTP {r.status_code}")
        return r.json()

    async def search(self, query: str, limit: int = 30) -> list[dict]:
        mc = await self.get("/search", params={"query": query, "limit": limit})
        results = mc.get("Metadata", [])
        # keep watchable things only
        return [m for m in results if m.get("type") in ("movie", "show", "season", "episode", "album", "artist")][:limit]


async def get_client() -> PlexClient:
    """Build a client from saved settings, falling back to env vars for first run."""
    url = await db.get_setting("plex_url", ENV_PLEX_URL)
    token = await db.get_setting("plex_token", ENV_PLEX_TOKEN)
    return PlexClient(url or "", token or "")
