"""Background job queue: runs one ffmpeg job at a time (locally or over SSH),
streams progress into the DB, and cleans up partial output on failure/cancel."""
import asyncio
import json
import logging
import os
import time

from . import db, executor as ex, ffmpeg_cmd
from .config import UPLOADS_DIR

log = logging.getLogger("mediaforge.runner")

LOG_TAIL = 400          # stderr lines kept per job
FLUSH_SECONDS = 2.0     # how often progress/log are written to the DB


class JobRunner:
    def __init__(self):
        self.current_id: int | None = None
        self._proc = None
        self._cancel_requested: set[int] = set()

    async def cancel(self, job_id: int) -> bool:
        """Cancel a queued or running job. Returns True if something was canceled."""
        job = await db.get_job(job_id)
        if job is None:
            return False
        if job["status"] == "queued":
            await db.update_job(job_id, status="canceled",
                                error="Canceled before it started",
                                finished_at=int(time.time()))
            return True
        if job["status"] == "running" and self.current_id == job_id:
            self._cancel_requested.add(job_id)
            if self._proc is not None:
                self._proc.kill()
            return True
        return False

    async def run(self) -> None:
        log.info("job runner started")
        while True:
            try:
                job = await db.next_queued_job()
                if job is None:
                    await asyncio.sleep(2)
                    continue
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job runner loop error")
                await asyncio.sleep(5)

    async def _process(self, job) -> None:
        job_id = job["id"]
        options = json.loads(job["options"])
        self.current_id = job_id
        self._proc = None
        await db.update_job(job_id, status="running", progress=0,
                            started_at=int(time.time()), error="")
        lines: list[str] = []

        def note(msg: str) -> None:
            lines.append(msg)

        exec_ = None
        staged_remote = None
        try:
            cfg = await ex.get_exec_settings()
            exec_ = await ex.get_executor(cfg, mode=job["mode"])
            in_path, out_path = job["input_path"], job["output_path"]
            note(f"mode: {job['mode']}  input: {in_path}")
            note(f"output: {out_path}")

            if not await exec_.exists(in_path):
                raise ex.ExecError(f"Input file not found: {in_path} "
                                   "(check the path mappings in Settings)")
            if await exec_.exists(out_path):
                raise ex.ExecError(f"Output already exists: {out_path} — "
                                   "pick a different suffix.")

            # subtitle staging: an uploaded file lives in UPLOADS_DIR; in SSH
            # mode it must be copied to the Plex server first
            sub_path = None
            if job["kind"] == "embed_sub":
                staged_name = options.get("staged_sub")
                if staged_name:
                    local = str(UPLOADS_DIR / staged_name)
                    if not os.path.exists(local):
                        raise ex.ExecError("The uploaded subtitle file is missing — re-queue the job.")
                    if job["mode"] == "ssh":
                        staged_remote = await exec_.staging_path(f"mediaforge_{job_id}_{staged_name}")
                        note(f"copying subtitle to the Plex server: {staged_remote}")
                        sub_path = await exec_.put_file(local, staged_remote)
                    else:
                        sub_path = local
                else:
                    sub_path = exec_.map(options.get("sub_path") or "")
                    if not sub_path or not await exec_.exists(sub_path):
                        raise ex.ExecError(f"Subtitle file not found: {sub_path or '(empty)'}")

            probe = await exec_.probe(in_path)
            duration = ffmpeg_cmd.duration_ms(probe) or job["duration_ms"] or 0
            argv = ffmpeg_cmd.build(job["kind"], probe, in_path, out_path, options, sub_path)
            note("ffmpeg " + " ".join(argv))

            proc = await exec_.start_ffmpeg(argv)
            self._proc = proc

            state = {"progress": 0.0, "flushed": 0.0}

            async def read_progress():
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
                    if line.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            us = int(line.split("=", 1)[1])
                            if duration > 0:
                                state["progress"] = min(99.0, us / 1000.0 / duration * 100.0)
                        except ValueError:
                            pass

            async def read_log():
                while True:
                    raw = await proc.stderr.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip() if isinstance(raw, bytes) else raw.rstrip()
                    if line:
                        lines.append(line)
                        del lines[:-LOG_TAIL]

            async def flusher():
                while True:
                    await asyncio.sleep(FLUSH_SECONDS)
                    if state["progress"] != state["flushed"]:
                        state["flushed"] = state["progress"]
                        await db.update_job(job_id, progress=state["progress"],
                                            log="\n".join(lines))

            readers = asyncio.gather(read_progress(), read_log())
            flush_task = asyncio.create_task(flusher())
            try:
                await readers
                rc = await proc.wait()
            finally:
                flush_task.cancel()

            if job_id in self._cancel_requested:
                raise _Canceled()
            if rc != 0:
                tail = "\n".join(lines[-6:])
                raise ex.ExecError(f"ffmpeg exited with code {rc}:\n{tail}")
            if not await exec_.exists(out_path):
                raise ex.ExecError("ffmpeg finished but the output file was not created.")

            await db.update_job(job_id, status="done", progress=100,
                                log="\n".join(lines), finished_at=int(time.time()))
            log.info("job %s done: %s", job_id, out_path)

        except _Canceled:
            note("job canceled — removing partial output")
            await self._cleanup_output(exec_, job["output_path"])
            await db.update_job(job_id, status="canceled", error="Canceled",
                                log="\n".join(lines), finished_at=int(time.time()))
        except (ex.ExecError, ffmpeg_cmd.BuildError) as e:
            log.warning("job %s failed: %s", job_id, e)
            await self._cleanup_output(exec_, job["output_path"])
            await db.update_job(job_id, status="error", error=str(e),
                                log="\n".join(lines), finished_at=int(time.time()))
        except Exception as e:
            log.exception("job %s crashed", job_id)
            await self._cleanup_output(exec_, job["output_path"])
            await db.update_job(job_id, status="error", error=f"Unexpected error: {e}",
                                log="\n".join(lines), finished_at=int(time.time()))
        finally:
            if exec_ is not None and staged_remote:
                try:
                    await exec_.cleanup_staged(staged_remote)
                except Exception:
                    pass
            if exec_ is not None:
                await exec_.close()
            self._cancel_requested.discard(job_id)
            self.current_id = None
            self._proc = None

    async def _cleanup_output(self, exec_, out_path: str) -> None:
        """Never leave a half-written file next to the original."""
        if exec_ is None:
            return
        try:
            if await exec_.exists(out_path):
                await exec_.remove(out_path)
        except Exception:
            log.warning("could not remove partial output %s", out_path)


class _Canceled(Exception):
    pass


runner = JobRunner()
