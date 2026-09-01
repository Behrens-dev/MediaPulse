"""App configuration. All runtime state lives in DATA_DIR (a mounted volume in Docker)."""
import os
from pathlib import Path

APP_VERSION = "0.3.0"

DATA_DIR = Path(os.environ.get("MEDIAFORGE_DATA", "/config"))
DB_PATH = DATA_DIR / "mediaforge.db"
UPLOADS_DIR = DATA_DIR / "uploads"

HOST = os.environ.get("MEDIAFORGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("MEDIAFORGE_PORT", "8191"))

# Optional initial values; once saved from the UI, the DB wins.
ENV_PLEX_URL = os.environ.get("PLEX_URL", "")
ENV_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
