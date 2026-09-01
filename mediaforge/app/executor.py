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
        "ssh_os": await db.get_setting("ssh_os", "linux"),
        "ssh_auth": await db.get_setting("ssh_auth", "password"),
        "ssh_password": await db.get_setting("ssh_password", ""),
        "ssh_key": await db.get_setting("ssh_key", ""),
        "ssh_key_passphrase": await db.get_setting("ssh_key_passphrase", ""),
        "path_maps_local": await db.get_setting("path_maps_local", []),
        "path_maps_ssh": await db.get_setting("path_maps_ssh", []),
    }


def map_path(path: str, maps: list[dict]) -> str:
    """Translate a path as Plex reports it into a path the executor can open.
    Longest matching prefix wins; no match means the path is used as-is.

    Windows-style paths (a Plex server on Windows reports e.g. Z:\\Movies\\...)
    are normalized to forward slashes and matched case-insensitively."""
    norm = path.replace("\\", "/")
    best = None
    for m in maps or []:
        src = (m.get("plex") or "").replace("\\", "/").rstrip("/")
        if src and (norm.lower() == src.lower()
                    or norm.lower().startswith(src.lower() + "/")):
            if best is None or len(src) > len(best[0]):
                best = (src, (m.get("local") or "").rstrip("/"))
    if best is None:
        return path
    return best[1] + norm[len(best[0]):]


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
    account or service account (password or private key). Supports Linux
    servers and Windows servers running OpenSSH (cmd or PowerShell shell —
    detected automatically). File checks/deletes/uploads go over SFTP, which
    both platforms ship with, so they don't depend on the remote shell."""
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
        self.windows = (cfg.get("ssh_os") or "linux") == "windows"
        self.maps = cfg.get("path_maps_ssh") or []
        self._conn = None
        self._sftp = None
        self._temp_dir: str | None = None
        self._win_shell: str | None = None   # "cmd" | "powershell", detected once

    def map(self, path: str) -> str:
        return map_path(path, self.maps)

    def _cmd(self, argv: list[str]) -> str:
        """Build a shell command string for the remote server's shell."""
        if self.windows:
            # double-quote every arg; our args never contain quotes, and
            # neither cmd nor PowerShell interpret | or & inside double quotes
            quoted = " ".join(f'"{a}"' for a in argv)
            # PowerShell needs the call operator to run a quoted command name
            return ("& " + quoted) if self._win_shell == "powershell" else quoted
        return " ".join(shlex.quote(a) for a in argv)

    async def _ensure_win_shell(self) -> None:
        """Figure out (once) whether the Windows SSH shell is cmd or PowerShell,
        and resolve the remote temp directory along the way."""
        if not self.windows or self._win_shell is not None:
            return
        rc, out, _ = await self._run_raw("echo %TEMP%", timeout=30.0)
        out = (out or "").strip()
        if rc == 0 and out and "%" not in out:
            self._win_shell = "cmd"
            self._temp_dir = out
        else:
            self._win_shell = "powershell"
            rc, out, _ = await self._run_raw("echo $env:TEMP", timeout=30.0)
            out = (out or "").strip()
            self._temp_dir = out if rc == 0 and out else "C:\\Windows\\Temp"

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

    async def _run_raw(self, cmd: str, timeout: float = 120.0) -> tuple[int, str, str]:
        conn = await self._connect()
        try:
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        except asyncio.TimeoutError:
            raise ExecError(f"Remote command timed out: {cmd.split()[0]}")
        return result.exit_status, result.stdout or "", result.stderr or ""

    async def _run(self, argv: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        await self._ensure_win_shell()
        return await self._run_raw(self._cmd(argv), timeout)

    async def _sftp_client(self):
        if self._sftp is None:
            conn = await self._connect()
            try:
                self._sftp = await conn.start_sftp_client()
            except Exception as e:
                raise ExecError(f"Could not start SFTP on the server: {e}") from e
        return self._sftp

    @staticmethod
    def _sftp_path(path: str) -> str:
        """Windows OpenSSH's SFTP server wants forward slashes."""
        return path.replace("\\", "/")

    async def probe(self, path: str) -> dict:
        rc, out, err = await self._run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
        if rc != 0:
            raise ExecError(f"ffprobe failed on the Plex server: {err.strip() or 'unknown error'}")
        return json.loads(out)

    async def exists(self, path: str) -> bool:
        sftp = await self._sftp_client()
        try:
            return await sftp.exists(self._sftp_path(path))
        except Exception as e:
            raise ExecError(f"Could not check {path} over SFTP: {e}") from e

    async def remove(self, path: str) -> None:
        sftp = await self._sftp_client()
        try:
            await sftp.remove(self._sftp_path(path))
        except Exception:
            pass

    async def staging_path(self, name: str) -> str:
        """A temp-file path on the remote server for staged uploads."""
        if not self.windows:
            return f"/tmp/{name}"
        await self._ensure_win_shell()
        return f"{self._temp_dir}\\{name}"

    async def put_file(self, local_path: str, dest_path: str) -> str:
        """Copy a staged file (e.g. an uploaded subtitle) to the Plex server."""
        sftp = await self._sftp_client()
        try:
            await sftp.put(local_path, self._sftp_path(dest_path))
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
        await self._ensure_win_shell()
        cmd = self._cmd(["ffmpeg"] + argv)
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
