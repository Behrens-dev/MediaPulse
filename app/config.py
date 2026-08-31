"""App configuration. All runtime state lives in DATA_DIR (a mounted volume in Docker)."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PLEXPULSE_DATA", "/config"))
DB_PATH = DATA_DIR / "plexpulse.db"

HOST = os.environ.get("PLEXPULSE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PLEXPULSE_PORT", "8181"))

# Optional initial values; once saved from the UI, the DB wins.
ENV_PLEX_URL = os.environ.get("PLEX_URL", "")
ENV_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

SESSION_POLL_SECONDS = int(os.environ.get("PLEXPULSE_POLL_SECONDS", "15"))
HEALTH_POLL_SECONDS = int(os.environ.get("PLEXPULSE_HEALTH_SECONDS", "60"))
# consecutive failed health checks before a "server down" email fires
HEALTH_FAILURE_THRESHOLD = int(os.environ.get("PLEXPULSE_HEALTH_THRESHOLD", "3"))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
