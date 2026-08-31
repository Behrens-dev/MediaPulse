"""Thin async client for the Plex Media Server HTTP API (JSON responses)."""
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
            "X-Plex-Client-Identifier": "plexpulse",
            "X-Plex-Product": "PlexPulse",
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
