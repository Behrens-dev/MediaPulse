"""MediaForge — ffmpeg toolbox for your Plex library (downmixing, subtitles,
track surgery, and A/V realignment), run in-container or on the Plex server."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, update_check
from .api import router
from .config import ensure_data_dir
from .runner import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_dir()
    await db.get_db()
    # any job left "running" by an unclean shutdown can never finish — mark it
    conn = await db.get_db()
    await conn.execute(
        "UPDATE jobs SET status='error', error='Interrupted by a MediaForge restart' "
        "WHERE status='running'")
    await conn.commit()
    tasks = [
        asyncio.create_task(runner.run()),
        asyncio.create_task(update_check.run()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await db.close_db()


app = FastAPI(title="MediaForge", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
