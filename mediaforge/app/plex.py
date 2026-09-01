"""Thin async client for the Plex Media Server HTTP API (JSON responses)."""
import httpx

from . import db
from .config import ENV_PLEX_URL, ENV_PLEX_TOKEN


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
            "X-Plex-Client-Identifier": "mediaforge",
            "X-Plex-Product": "MediaForge",
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

    async def search(self, query: str, limit: int = 30) -> list[dict]:
        mc = await self.get("/search", params={"query": query, "limit": limit})
        results = mc.get("Metadata", [])
        return [m for m in results
                if m.get("type") in ("movie", "show", "season", "episode")][:limit]

    async def metadata(self, rating_key: str) -> dict:
        """Full metadata for one item, incl. per-file Media/Part/Stream elements."""
        mc = await self.get(f"/library/metadata/{rating_key}", timeout=60.0)
        items = mc.get("Metadata", [])
        if not items:
            raise PlexError(f"Item {rating_key} not found")
        return items[0]

    async def leaves(self, rating_key: str) -> list[dict]:
        """All leaf episodes under a show or season."""
        mc = await self.get(f"/library/metadata/{rating_key}/allLeaves", timeout=120.0)
        return mc.get("Metadata", [])

    async def poster(self, thumb_path: str, width: int = 240, height: int = 360) -> tuple[bytes, str]:
        return await self.get_bytes(
            "/photo/:/transcode",
            params={"url": thumb_path, "width": width, "height": height, "minSize": 1},
        )


async def get_client() -> PlexClient:
    """Build a client from saved settings, falling back to env vars for first run."""
    url = await db.get_setting("plex_url", ENV_PLEX_URL)
    token = await db.get_setting("plex_token", ENV_PLEX_TOKEN)
    return PlexClient(url or "", token or "")
