"""All REST endpoints consumed by the web UI."""
import base64
import binascii
import json
import os
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Query, Response

from . import db, executor as ex, ffmpeg_cmd, plex, update_check
from .config import APP_VERSION, GIT_SHA, UPLOADS_DIR
from .runner import runner

router = APIRouter(prefix="/api")


async def _update_info() -> dict:
    st = await db.get_setting("update_status", {}) or {}
    return {
        "enabled": bool(await db.get_setting("update_check_enabled", True)),
        "available": bool(st.get("available")),
        "current": GIT_SHA[:7],
        "latest": (st.get("latest_sha") or "")[:7],
        "checked_at": st.get("checked_at") or 0,
        "error": st.get("error") or "",
    }


def _err(e: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(e))


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


# ---------- status / settings ----------

@router.get("/status")
async def status():
    client = await plex.get_client()
    info = {}
    plex_ok, plex_error = False, ""
    if client.configured:
        try:
            mc = await client.server_info()
            info = {"name": mc.get("friendlyName"), "version": mc.get("version")}
            plex_ok = True
        except plex.PlexError as e:
            plex_error = str(e)
    mode = await db.get_setting("exec_mode", "local")
    conn = await db.get_db()
    async with conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ) as cur:
        counts = {r["status"]: r["n"] for r in await cur.fetchall()}
    return {
        "version": APP_VERSION,
        "configured": client.configured,
        "plex_ok": plex_ok,
        "plex_error": plex_error,
        "server": info,
        "mode": mode,
        "jobs": counts,
        "running_id": runner.current_id,
        "update": await _update_info(),
    }


@router.post("/update-check")
async def run_update_check():
    if not GIT_SHA:
        return {"ok": False, "error": "This build has no version stamp (dev build) — "
                                      "update checks only work on the published image."}
    await update_check.check_now()
    return {"ok": True, **(await _update_info())}


SETTING_KEYS = [
    "plex_url", "plex_token",
    "exec_mode",
    "ssh_host", "ssh_port", "ssh_username", "ssh_os", "ssh_auth",
    "ssh_password", "ssh_key", "ssh_key_passphrase",
    "path_maps_local", "path_maps_ssh", "update_check_enabled",
]

DEFAULTS = {
    "exec_mode": "local",
    "ssh_port": 22,
    "ssh_os": "linux",
    "ssh_auth": "password",
    "update_check_enabled": True,
    "path_maps_local": [],
    "path_maps_ssh": [],
}


@router.get("/settings")
async def get_settings():
    out = {}
    for k in SETTING_KEYS:
        out[k] = await db.get_setting(k, DEFAULTS.get(k, ""))
    if not out["plex_url"]:
        client = await plex.get_client()  # picks up env-var fallback
        out["plex_url"], out["plex_token"] = client.base_url, client.token
    return out


@router.post("/settings")
async def save_settings(payload: dict):
    if payload.get("exec_mode") == "ssh":
        if not (payload.get("ssh_host") or "").strip() or not (payload.get("ssh_username") or "").strip():
            raise _bad("Running on the Plex server requires an SSH host and an "
                       "account / service account username.")
        auth = payload.get("ssh_auth", "password")
        has_pw = bool((payload.get("ssh_password") or "").strip()) \
            or bool(await db.get_setting("ssh_password", ""))
        has_key = bool((payload.get("ssh_key") or "").strip()) \
            or bool(await db.get_setting("ssh_key", ""))
        if auth == "password" and not has_pw:
            raise _bad("Password authentication selected — enter the account's password.")
        if auth == "key" and not has_key:
            raise _bad("Key authentication selected — paste the account's private key.")
    for k, v in payload.items():
        if k in SETTING_KEYS:
            await db.set_setting(k, v)
    return {"ok": True}


@router.post("/settings/test-plex")
async def test_plex(payload: dict):
    client = plex.PlexClient(payload.get("plex_url", ""), payload.get("plex_token", ""))
    try:
        mc = await client.server_info()
        return {"ok": True, "name": mc.get("friendlyName"), "version": mc.get("version")}
    except plex.PlexError as e:
        return {"ok": False, "error": str(e)}


@router.post("/settings/test-exec")
async def test_exec(payload: dict):
    """Check that ffmpeg/ffprobe are reachable in the chosen mode. Unsaved SSH
    fields in the payload override saved ones so 'Test' works before 'Save'."""
    cfg = await ex.get_exec_settings()
    for k in ("ssh_host", "ssh_port", "ssh_username", "ssh_os", "ssh_auth",
              "ssh_password", "ssh_key", "ssh_key_passphrase"):
        v = payload.get(k)
        if v not in (None, ""):
            cfg[k] = v
    cfg["ssh_port"] = int(cfg.get("ssh_port") or 22)
    mode = payload.get("mode") or cfg["mode"]
    exec_ = None
    try:
        exec_ = await ex.get_executor(cfg, mode=mode)
        versions = await exec_.versions()
        return {"ok": True, "mode": mode, **versions}
    except ex.ExecError as e:
        return {"ok": False, "mode": mode, "error": str(e)}
    finally:
        if exec_ is not None:
            await exec_.close()


# ---------- library browsing ----------

@router.get("/search")
async def search(q: str = Query(..., min_length=2)):
    client = await plex.get_client()
    try:
        results = await client.search(q)
    except plex.PlexError as e:
        raise _err(e)
    return [{
        "rating_key": m.get("ratingKey"),
        "type": m.get("type"),
        "title": m.get("title"),
        "year": m.get("year"),
        "parent_title": m.get("parentTitle"),
        "grandparent_title": m.get("grandparentTitle"),
        "thumb": m.get("grandparentThumb") or m.get("thumb") or "",
    } for m in results]


def _display_title(m: dict) -> str:
    if m.get("type") == "episode":
        season = m.get("parentIndex")
        epnum = m.get("index")
        code = f"S{int(season):02d}E{int(epnum):02d} · " if season is not None and epnum is not None else ""
        return f"{m.get('grandparentTitle')} — {code}{m.get('title')}"
    year = f" ({m['year']})" if m.get("year") else ""
    return f"{m.get('title')}{year}"


def _files_from_metadata(m: dict) -> list[dict]:
    files = []
    for media in m.get("Media") or []:
        for part in media.get("Part") or []:
            streams = part.get("Stream") or []
            files.append({
                "path": part.get("file"),
                "size": part.get("size"),
                "container": part.get("container"),
                "video": next((f"{s.get('displayTitle') or s.get('codec', '?')}"
                               for s in streams if s.get("streamType") == 1), ""),
                "audio": [{
                    "codec": (s.get("codec") or "?").upper(),
                    "channels": s.get("channels"),
                    "language": s.get("language") or s.get("languageTag") or "",
                    "title": s.get("displayTitle") or s.get("title") or "",
                } for s in streams if s.get("streamType") == 2],
                "subs": [{
                    "codec": (s.get("codec") or "?").upper(),
                    "language": s.get("language") or s.get("languageTag") or "",
                    "title": s.get("displayTitle") or s.get("title") or "",
                } for s in streams if s.get("streamType") == 3],
            })
    return files


@router.get("/item/{rating_key}")
async def item(rating_key: str):
    client = await plex.get_client()
    try:
        m = await client.metadata(rating_key)
        out = {
            "rating_key": m.get("ratingKey"),
            "type": m.get("type"),
            "title": _display_title(m),
            "thumb": m.get("grandparentThumb") or m.get("thumb") or "",
            "files": [],
            "episodes": [],
        }
        if m.get("type") in ("show", "season"):
            leaves = await client.leaves(rating_key)
            out["episodes"] = [{
                "rating_key": e.get("ratingKey"),
                "title": _display_title(e),
                "season": e.get("parentIndex"),
                "episode": e.get("index"),
            } for e in leaves]
        else:
            out["files"] = _files_from_metadata(m)
        return out
    except plex.PlexError as e:
        raise _err(e)


@router.get("/poster")
async def poster(thumb: str, w: int = 120, h: int = 180):
    client = await plex.get_client()
    try:
        body, ctype = await client.poster(thumb, w, h)
    except plex.PlexError:
        raise HTTPException(status_code=404, detail="poster unavailable")
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "max-age=3600"})


# ---------- probing ----------

@router.post("/probe")
async def probe(payload: dict):
    """ffprobe a file (by its Plex path) in the active execution mode."""
    path = (payload.get("path") or "").strip()
    if not path:
        raise _bad("No file path given.")
    exec_ = None
    try:
        cfg = await ex.get_exec_settings()
        exec_ = await ex.get_executor(cfg)
        mapped = exec_.map(path)
        if exec_.mode == "ssh":
            where = (f"over SFTP on {cfg.get('ssh_host')} "
                     f"(server OS: {cfg.get('ssh_os', 'linux')})")
        else:
            where = "inside the MediaForge container (local mode)"
        if not await exec_.exists(mapped):
            return {"ok": False, "mapped_path": mapped, "mode": exec_.mode,
                    "error": f"File not found at {mapped} — checked {where}. "
                             "If the mode or path looks wrong, fix it in Settings."}
        data = await exec_.probe(mapped)
        return {
            "ok": True,
            "mode": exec_.mode,
            "mapped_path": mapped,
            "duration_ms": ffmpeg_cmd.duration_ms(data),
            "size": int(data.get("format", {}).get("size") or 0),
            "container": data.get("format", {}).get("format_name", ""),
            "audio": ffmpeg_cmd.audio_streams(data),
            "subs": ffmpeg_cmd.subtitle_streams(data),
        }
    except ex.ExecError as e:
        return {"ok": False, "error": str(e), "mode": exec_.mode if exec_ else "?"}
    finally:
        if exec_ is not None:
            await exec_.close()


# ---------- jobs ----------

SUB_UPLOAD_MAX = 10 * 1024 * 1024


@router.post("/jobs")
async def create_job(payload: dict):
    kind = payload.get("kind")
    if kind not in ("downmix", "convert", "embed_sub", "remove_audio", "audio_sync", "sub_sync"):
        raise _bad(f"Unknown job kind: {kind}")
    plex_path = (payload.get("path") or "").strip()
    if not plex_path:
        raise _bad("No file selected.")
    suffix = ffmpeg_cmd.sanitize_suffix(payload.get("suffix") or "")
    if not suffix:
        raise _bad("A filename suffix is required so the output never collides "
                   "with the original (e.g. _encoded).")
    options = dict(payload.get("options") or {})

    cfg = await ex.get_exec_settings()
    mode = cfg["mode"]
    exec_ = None
    try:
        exec_ = await ex.get_executor(cfg)
        in_path = exec_.map(plex_path)
        if not await exec_.exists(in_path):
            raise _bad(f"Input file not found at {in_path}. "
                       "Check the path mappings in Settings.")
        ext = None
        if kind == "convert":
            cont = (options.get("container") or "keep").lower()
            if cont in ("mkv", "mp4"):
                ext = "." + cont
        out_path = ffmpeg_cmd.output_path(in_path, suffix, ext)
        if await exec_.exists(out_path):
            raise _bad(f"{os.path.basename(out_path)} already exists — pick a different suffix.")

        # stage an uploaded subtitle file (kept in the config volume until the job runs)
        sub_path = None
        if kind == "embed_sub":
            b64 = options.pop("sub_b64", None)
            name = options.pop("sub_name", "") or ""
            if b64:
                ext = os.path.splitext(name)[1].lower()
                if ext not in ffmpeg_cmd.TEXT_SUB_EXTS:
                    raise _bad(f"Unsupported subtitle type '{ext or '(none)'}'. "
                               f"Use one of: {', '.join(ffmpeg_cmd.TEXT_SUB_EXTS)}")
                try:
                    raw = base64.b64decode(b64)
                except (binascii.Error, ValueError):
                    raise _bad("Could not decode the uploaded subtitle file.")
                if len(raw) > SUB_UPLOAD_MAX:
                    raise _bad("Subtitle file too large (max 10 MB).")
                staged = f"{uuid.uuid4().hex}{ext}"
                (UPLOADS_DIR / staged).write_bytes(raw)
                options["staged_sub"] = staged
                sub_path = str(UPLOADS_DIR / staged)
            elif options.get("sub_path"):
                sub_path = exec_.map(options["sub_path"])
                if not await exec_.exists(sub_path):
                    raise _bad(f"Subtitle file not found at {sub_path}.")
            else:
                raise _bad("Provide a subtitle file — upload one or give its path.")

        # dry-run the command builder now so bad options fail fast, with a clear message
        probe_data = await exec_.probe(in_path)
        try:
            ffmpeg_cmd.build(kind, probe_data, in_path, out_path, options, sub_path)
        except ffmpeg_cmd.BuildError as e:
            raise _bad(str(e))

        # run now (default) or hold in the queue until a scheduled start time
        schedule = payload.get("schedule") or {}
        status, run_at = "queued", None
        if schedule.get("mode") == "at":
            try:
                run_at = int(schedule.get("run_at"))
            except (TypeError, ValueError):
                raise _bad("The scheduled start time is missing or invalid.")
            if run_at > int(time.time()):
                status = "scheduled"
            else:
                run_at = None  # time already passed — just queue it now

        job_id = await db.create_job(
            kind=kind, title=payload.get("title") or os.path.basename(plex_path),
            rating_key=str(payload.get("rating_key") or ""),
            plex_path=plex_path, input_path=in_path, output_path=out_path,
            suffix=suffix, options=options, mode=mode,
            duration_ms=ffmpeg_cmd.duration_ms(probe_data),
            status=status, run_at=run_at,
        )
        return {"ok": True, "id": job_id, "output": out_path,
                "status": status, "run_at": run_at}
    except ex.ExecError as e:
        raise _err(e)
    finally:
        if exec_ is not None:
            await exec_.close()


@router.get("/jobs")
async def jobs(limit: int = 100):
    rows = await db.list_jobs(limit)
    return [dict(r) for r in rows]


@router.get("/jobs/{job_id}")
async def job_detail(job_id: int):
    row = await db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    d = dict(row)
    d["options"] = json.loads(d["options"])
    return d


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    ok = await runner.cancel(job_id)
    if not ok:
        raise _bad("Job is not queued or running.")
    return {"ok": True}


@router.post("/jobs/{job_id}/start")
async def start_job(job_id: int):
    """Release a scheduled job into the queue right now."""
    row = await db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row["status"] != "scheduled":
        raise _bad("Only scheduled jobs can be started early.")
    await db.update_job(job_id, status="queued", run_at=None)
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int):
    row = await db.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row["status"] not in ("error", "canceled"):
        raise _bad("Only failed or canceled jobs can be retried.")
    await db.update_job(job_id, status="queued", progress=0, error="", log="",
                        started_at=None, finished_at=None)
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def remove_job(job_id: int):
    row = await db.get_job(job_id)
    if row is None:
        return {"ok": True}
    if row["status"] == "running":
        raise _bad("Cancel the job before deleting it.")
    # drop any staged subtitle upload that belonged to this job
    staged = (json.loads(row["options"]) or {}).get("staged_sub")
    if staged and re.fullmatch(r"[0-9a-f]{32}\.[a-z]+", staged):
        try:
            (UPLOADS_DIR / staged).unlink(missing_ok=True)
        except OSError:
            pass
    await db.delete_job(job_id)
    return {"ok": True}
