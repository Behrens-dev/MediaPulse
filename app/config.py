"""App configuration. All runtime state lives in DATA_DIR (a mounted volume in Docker)."""
import os
from pathlib import Path


def _env(name: str, legacy: str, default: str) -> str:
    """Read MEDIAPULSE_* with a fallback to the pre-rename PLEXPULSE_* name."""
    return os.environ.get(name, os.environ.get(legacy, default))


DATA_DIR = Path(_env("MEDIAPULSE_DATA", "PLEXPULSE_DATA", "/config"))
# existing installs keep their plexpulse.db; fresh installs get mediapulse.db
_LEGACY_DB = DATA_DIR / "plexpulse.db"
DB_PATH = _LEGACY_DB if _LEGACY_DB.exists() else DATA_DIR / "mediapulse.db"

HOST = _env("MEDIAPULSE_HOST", "PLEXPULSE_HOST", "0.0.0.0")
PORT = int(_env("MEDIAPULSE_PORT", "PLEXPULSE_PORT", "8181"))

# baked in by the GitHub Actions build (--build-arg GIT_SHA=...); empty in dev
GIT_SHA = os.environ.get("GIT_SHA", "")
UPDATE_REPO = os.environ.get("UPDATE_REPO", "Behrens-dev/MediaPulse")

# Optional initial values; once saved from the UI, the DB wins.
ENV_PLEX_URL = os.environ.get("PLEX_URL", "")
ENV_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

SESSION_POLL_SECONDS = int(_env("MEDIAPULSE_POLL_SECONDS", "PLEXPULSE_POLL_SECONDS", "15"))
HEALTH_POLL_SECONDS = int(_env("MEDIAPULSE_HEALTH_SECONDS", "PLEXPULSE_HEALTH_SECONDS", "60"))
# consecutive failed health checks before a "server down" email fires
HEALTH_FAILURE_THRESHOLD = int(_env("MEDIAPULSE_HEALTH_THRESHOLD", "PLEXPULSE_HEALTH_THRESHOLD", "3"))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
