"""PlexPulse — Plex monitoring, library analytics, and notifications."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router
from .config import ensure_data_dir
from .poller import activity_poller, health_monitor, newsletter_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_dir()
    await db.get_db()
    tasks = [
        asyncio.create_task(activity_poller.run()),
        asyncio.create_task(health_monitor.run()),
        asyncio.create_task(newsletter_scheduler.run()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await db.close_db()


app = FastAPI(title="PlexPulse", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
