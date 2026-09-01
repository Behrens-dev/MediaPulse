"""Where ffmpeg actually runs: inside this container ("local") or on the Plex
server over SSH ("ssh"). Both executors expose the same small surface so the
job runner and API don't care which one is active."""
import asyncio
import json
import logging
import shlex

from . import db

log = logging.getLogger("mediaforge.executor")


class ExecError(Exception):
    pass


async def get_exec_settings() -> dict:
    return {
        "mode": await db.get_setting("exec_mode", "local"),
        "ssh_host": await db.get_setting("ssh_host", ""),
        "ssh_port": int(await db.get_setting("ssh_port", 22) or 22),
        "ssh_username": await db.get_setting("ssh_username", ""),
        "ssh_auth": await db.get_setting("ssh_auth", "password"),
        "ssh_password": await db.get_setting("ssh_password", ""),
        "ssh_key": await db.get_setting("ssh_key", ""),
        "ssh_key_passphrase": await db.get_setting("ssh_key_passphrase", ""),
        "path_maps_local": await db.get_setting("path_maps_local", []),
        "path_maps_ssh": await db.get_setting("path_maps_ssh", []),
    }


def map_path(path: str, maps: list[dict]) -> str:
    """Translate a path as Plex reports it into a path the executor can open.
    Longest matching prefix wins; no match means the path is used as-is."""
    best = None
    for m in maps or []:
        src = (m.get("plex") or "").rstrip("/")
        if src and (path == src or path.startswith(src + "/")):
            if best is None or len(src) > len(best[0]):
                best = (src, (m.get("local") or "").rstrip("/"))
    if best is None:
        return path
    return best[1] + path[len(best[0]):]


class FfmpegProcess:
    """Uniform handle over a local subprocess or a remote (asyncssh) process."""

    def __init__(self, stdout, stderr, wait, kill):
        self.stdout = stdout      # async line reader (progress key=value pairs)
        self.stderr = stderr      # async line reader (ffmpeg log)
        self._wait = wait
        self._kill = kill

    async def wait(self) -> int:
        return await self._wait()

    def kill(self) -> None:
        try:
            self._kill()
        except Exception:
            pass


class LocalExecutor:
    mode = "local"

    def __init__(self, maps: list[dict]):
        self.maps = maps

    def map(self, path: str) -> str:
        return map_path(path, self.maps)

    async def _run(self, argv: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ExecError(f"Command timed out: {argv[0]}")
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def probe(self, path: str) -> dict:
        rc, out, err = await self._run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
        if rc != 0:
            raise ExecError(f"ffprobe failed: {err.strip() or 'unknown error'}")
        return json.loads(out)

    async def exists(self, path: str) -> bool:
        import os
        return await asyncio.to_thread(os.path.exists, path)

    async def remove(self, path: str) -> None:
        import os
        try:
            await asyncio.to_thread(os.remove, path)
        except OSError:
            pass

    async def put_file(self, local_path: str, dest_path: str) -> str:
        """Local mode: the staged file is already on this filesystem."""
        return local_path

    async def cleanup_staged(self, path: str) -> None:
        pass  # staged uploads live in DATA_DIR and are cleaned with the job

    async def versions(self) -> dict:
        rc, out, err = await self._run(["ffmpeg", "-version"], timeout=20.0)
        if rc != 0:
            raise ExecError(f"ffmpeg not available: {err.strip()}")
        rc2, out2, _ = await self._run(["ffprobe", "-version"], timeout=20.0)
        return {
            "ffmpeg": out.splitlines()[0] if out else "?",
            "ffprobe": out2.splitlines()[0] if rc2 == 0 and out2 else "not found",
        }

    async def start_ffmpeg(self, argv: list[str]) -> FfmpegProcess:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def _wait():
            return await proc.wait()

        return FfmpegProcess(proc.stdout, proc.stderr, _wait, proc.kill)

    async def close(self) -> None:
        pass


class SSHExecutor:
    """Runs ffmpeg/ffprobe on the Plex server itself, authenticated with an
    account or service account (password or private key)."""
    mode = "ssh"

    def __init__(self, cfg: dict):
        if not cfg.get("ssh_host") or not cfg.get("ssh_username"):
            raise ExecError("SSH mode needs a host and username — set them in Settings.")
        if cfg.get("ssh_auth") == "key":
            if not cfg.get("ssh_key"):
                raise ExecError("SSH key authentication selected but no private key saved.")
        elif not cfg.get("ssh_password"):
            raise ExecError("SSH password authentication selected but no password saved.")
        self.cfg = cfg
        self.maps = cfg.get("path_maps_ssh") or []
        self._conn = None

    def map(self, path: str) -> str:
        return map_path(path, self.maps)

    async def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import asyncssh
        except ImportError as e:
            raise ExecError("asyncssh is not installed in this image") from e
        kwargs: dict = {
            "host": self.cfg["ssh_host"],
            "port": self.cfg["ssh_port"],
            "username": self.cfg["ssh_username"],
            "known_hosts": None,
        }
        if self.cfg.get("ssh_auth") == "key":
            try:
                key = asyncssh.import_private_key(
                    self.cfg["ssh_key"],
                    passphrase=self.cfg.get("ssh_key_passphrase") or None)
            except Exception as e:
                raise ExecError(f"Could not read the SSH private key: {e}") from e
            kwargs["client_keys"] = [key]
        else:
            kwargs["password"] = self.cfg["ssh_password"]
        try:
            self._conn = await asyncio.wait_for(asyncssh.connect(**kwargs), timeout=20.0)
        except asyncio.TimeoutError:
            raise ExecError(f"SSH connection to {self.cfg['ssh_host']} timed out")
        except Exception as e:
            raise ExecError(f"SSH connection failed: {e}") from e
        return self._conn

    async def _run(self, argv: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        conn = await self._connect()
        cmd = " ".join(shlex.quote(a) for a in argv)
        try:
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        except asyncio.TimeoutError:
            raise ExecError(f"Remote command timed out: {argv[0]}")
        return result.exit_status, result.stdout or "", result.stderr or ""

    async def probe(self, path: str) -> dict:
        rc, out, err = await self._run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
        if rc != 0:
            raise ExecError(f"ffprobe failed on the Plex server: {err.strip() or 'unknown error'}")
        return json.loads(out)

    async def exists(self, path: str) -> bool:
        rc, _, _ = await self._run(["test", "-e", path], timeout=30.0)
        return rc == 0

    async def remove(self, path: str) -> None:
        await self._run(["rm", "-f", path], timeout=30.0)

    async def put_file(self, local_path: str, dest_path: str) -> str:
        """Copy a staged file (e.g. an uploaded subtitle) to the Plex server."""
        conn = await self._connect()
        try:
            import asyncssh
            await asyncssh.scp(local_path, (conn, dest_path))
        except Exception as e:
            raise ExecError(f"Could not copy file to the Plex server: {e}") from e
        return dest_path

    async def cleanup_staged(self, path: str) -> None:
        await self.remove(path)

    async def versions(self) -> dict:
        rc, out, err = await self._run(["ffmpeg", "-version"], timeout=30.0)
        if rc != 0:
            raise ExecError("ffmpeg is not available on the Plex server over SSH: "
                            + (err.strip().splitlines()[0] if err.strip() else "not found"))
        rc2, out2, _ = await self._run(["ffprobe", "-version"], timeout=30.0)
        return {
            "ffmpeg": out.splitlines()[0] if out else "?",
            "ffprobe": out2.splitlines()[0] if rc2 == 0 and out2 else "not found",
        }

    async def start_ffmpeg(self, argv: list[str]) -> FfmpegProcess:
        conn = await self._connect()
        cmd = "ffmpeg " + " ".join(shlex.quote(a) for a in argv)
        proc = await conn.create_process(cmd, encoding=None)

        async def _wait():
            await proc.wait()
            return proc.exit_status if proc.exit_status is not None else -1

        def _kill():
            try:
                proc.terminate()
            except Exception:
                proc.close()

        return FfmpegProcess(proc.stdout, proc.stderr, _wait, _kill)

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


async def get_executor(cfg: dict | None = None, mode: str | None = None):
    """Build the active executor (or one for an explicit mode) from settings."""
    cfg = cfg or await get_exec_settings()
    mode = mode or cfg.get("mode", "local")
    if mode == "ssh":
        return SSHExecutor(cfg)
    return LocalExecutor(cfg.get("path_maps_local") or [])
