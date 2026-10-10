"""UDS supervisor. PID 1 owns cgroups; it cannot publish a download."""

from __future__ import annotations

import base64
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fetchnow.media_executor.argv import (
    ffprobe_argv,
    mux_argv,
    network_download_argv,
    network_inspect_argv,
)
from fetchnow.media_executor.constants import (
    INPUT_AUDIO,
    INPUT_VIDEO,
    MAX_JOBS,
    MAX_REQUEST_BYTES,
    OUTPUT_ARTIFACT_GLOB,
    OUTPUT_MUX,
    PROFILE_NETWORK,
    PROFILE_OFFLINE,
    TOOL_UID,
    WORKER_GID,
    WORKER_UID,
)
from fetchnow.media_executor.layout import (
    artifact_stat,
    job_directory,
    make_owned_directory,
)
from fetchnow.media_executor.protocol import (
    ProtocolError,
    Request,
    dump_response,
    parse_request,
)
from fetchnow.media_executor.runner import ToolOutcome, ToolRunner

_MAX_STDOUT = 196_608
_MAX_STDERR = 65_536
_OFFLINE_OPERATION_SECONDS = 120.0
_CONNECT_LIMIT = 8


@dataclass
class _Job:
    state: str
    op: str | None = None
    container: str | None = None
    url: str | None = None
    provider_id: str | None = None
    format_token: str | None = None
    max_bytes: int | None = None
    min_free_bytes: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None


class ExecutorApp:
    def __init__(
        self,
        *,
        work_root: Path,
        runner: ToolRunner,
        socket_dir: Path,
        profile: str = PROFILE_OFFLINE,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        ytdlp: str | None = None,
        proxy_url: str | None = None,
        download_timeout_seconds: float = 300.0,
        inspect_timeout_seconds: float = 30.0,
        socket_timeout_seconds: int = 30,
        max_filesize_bytes: int = 3_221_225_472,
        make_job: Callable[[Path], None] | None = None,
        remove_job: Callable[[Path], None] | None = None,
        peer_lookup: Callable[[socket.socket], int] | None = None,
    ) -> None:
        if profile not in {PROFILE_OFFLINE, PROFILE_NETWORK}:
            raise ValueError("profile")
        self.profile = profile
        self.work_root = work_root
        self.runner = runner
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.ytdlp = ytdlp
        self.proxy_url = proxy_url
        self.download_timeout_seconds = download_timeout_seconds
        self.inspect_timeout_seconds = inspect_timeout_seconds
        self.socket_timeout_seconds = socket_timeout_seconds
        self.max_filesize_bytes = max_filesize_bytes
        self.socket_dir = socket_dir
        self._make_job = make_job
        self._remove_job = remove_job
        self._peer_lookup = peer_lookup or peer_uid
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, int, int], _Job] = {}
        self._connections = threading.BoundedSemaphore(_CONNECT_LIMIT)

    def handle(self, raw: bytes, *, peer_uid: int) -> bytes:
        if peer_uid != WORKER_UID:
            return dump_response({"ok": False, "code": "unauthorized"})
        try:
            request = parse_request(raw, profile=self.profile)
        except ProtocolError as exc:
            return dump_response({"ok": False, "code": exc.code})
        try:
            payload = self._dispatch(request)
        except ProtocolError as exc:
            return dump_response({"ok": False, "code": exc.code})
        except OSError:
            return dump_response({"ok": False, "code": "failed"})
        return dump_response(payload)

    def _dispatch(self, request: Request) -> dict[str, object]:
        if request.op == "reserve":
            return self._reserve(request)
        if request.op == "cancel":
            return self._cancel(request)
        if request.op == "release":
            return self._release(request)
        if request.op in {
            "mux_copy",
            "ffprobe_validate",
            "inspect_metadata",
            "download_progressive",
            "download_video",
            "download_audio",
        }:
            return self._run_tool(request)
        raise ProtocolError("unknown_operation")

    def _reserve(self, request: Request) -> dict[str, object]:
        path = job_directory(self.work_root, request)
        with self._lock:
            job = self._jobs.get(request.key)
            if job is not None and job.state == "running":
                return {"ok": False, "code": "already_running"}
            if path.is_symlink():
                raise OSError("job directory symlink")
            if job is None:
                if path.exists():
                    raise OSError("stale job directory")
                maker = self._make_job
                if maker is None:
                    path.mkdir(mode=0o700)
                else:
                    maker(path)
                self._jobs[request.key] = _Job(state="reserved")
        return {"ok": True, "code": "reserved"}

    def _cancel(self, request: Request) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(request.key)
            if job is None or job.state != "running":
                return {"ok": True, "code": "not_running", "cancelled": False}
            job.cancel.set()
        return {"ok": True, "code": "cancelling", "cancelled": True}

    def _release(self, request: Request) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(request.key)
            if job is not None and job.state == "running":
                return {"ok": False, "code": "already_running"}
            if job is None:
                return {"ok": True, "code": "not_running"}
            if self._remove_job is not None:
                self._remove_job(job_directory(self.work_root, request))
                self._jobs.pop(request.key)
                return {"ok": True, "code": "released"}
            self._jobs.pop(request.key, None)
        path = job_directory(self.work_root, request)
        if path.is_symlink():
            raise OSError("job dir symlink")
        if path.is_dir():
            for child in path.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    for nested in child.iterdir():
                        if nested.is_symlink() or nested.is_file():
                            nested.unlink()
                        else:
                            raise OSError("unexpected job child")
                    child.rmdir()
                else:
                    raise OSError("unexpected job child")
            path.rmdir()
        return {"ok": True, "code": "released"}

    def _run_tool(self, request: Request) -> dict[str, object]:
        path = job_directory(self.work_root, request)
        with self._lock:
            running = sum(1 for job in self._jobs.values() if job.state == "running")
            current = self._jobs.get(request.key)
            if current is not None and current.state == "running":
                return {"ok": False, "code": "already_running"}
            if (
                current is not None
                and current.state == "done"
                and current.op == request.op
                and current.container == request.container
                and current.url == request.url
                and current.provider_id == request.provider_id
                and current.format_token == request.format_token
                and current.max_bytes == request.max_bytes
                and current.min_free_bytes == request.min_free_bytes
                and current.result is not None
            ):
                return current.result
            if running >= MAX_JOBS:
                return {"ok": False, "code": "overflow"}
            if current is None or current.state not in {"reserved", "done"}:
                return {"ok": False, "code": "not_reserved"}
            argv = self._argv(request, path)
            timeout = self._timeout_for(request.op)
            max_output, min_free = self._limits_for(request)
            current.state = "running"
            current.op = request.op
            current.container = request.container
            current.url = request.url
            current.provider_id = request.provider_id
            current.format_token = request.format_token
            current.max_bytes = request.max_bytes
            current.min_free_bytes = request.min_free_bytes
            current.cancel = threading.Event()
            cancel = current.cancel
        protected = [str(self.socket_dir), str(self.work_root)]
        try:
            outcome = self.runner.run(
                attempt=path,
                argv=argv,
                timeout_seconds=timeout,
                cancel=cancel,
                protected=protected,
                max_output_bytes=max_output,
                min_free_bytes=min_free,
            )
        except Exception:
            with self._lock:
                job = self._jobs[request.key]
                job.state = "reserved"
                job.op = None
            raise
        payload = _outcome_payload(outcome)
        if request.op.startswith("download_") and payload.get("ok") is True:
            artifact = _find_artifact_as_worker(path)
            if artifact is None:
                payload = {
                    "ok": False,
                    "code": "failed",
                    "exit_code": 1,
                    "timed_out": False,
                    "cancelled": False,
                    "stdout_b64": "",
                    "stderr_b64": "",
                }
            else:
                size = artifact[1]
                name = artifact[0]
                if request.max_bytes is not None and size > request.max_bytes:
                    payload = {
                        "ok": False,
                        "code": "failed",
                        "exit_code": 1,
                        "timed_out": False,
                        "cancelled": False,
                        "stdout_b64": "",
                        "stderr_b64": "",
                    }
                else:
                    payload["artifact_name"] = name
                    payload["artifact_bytes"] = size
        with self._lock:
            job = self._jobs[request.key]
            job.state = "done"
            job.result = payload
        return payload

    def _timeout_for(self, op: str) -> float:
        if op == "inspect_metadata":
            return self.inspect_timeout_seconds
        if op.startswith("download_"):
            return self.download_timeout_seconds
        return _OFFLINE_OPERATION_SECONDS

    def _limits_for(self, request: Request) -> tuple[int | None, int | None]:
        if not request.op.startswith("download_"):
            return None, None
        if request.max_bytes is None or request.min_free_bytes is None:
            raise ProtocolError("malformed")
        # Worker cannot raise the server ceiling.
        return min(request.max_bytes, self.max_filesize_bytes), request.min_free_bytes

    def _argv(self, request: Request, path: Path) -> list[str]:
        if request.op == "mux_copy":
            assert request.container is not None
            assert self.ffmpeg is not None
            return mux_argv(
                ffmpeg=self.ffmpeg,
                video=path / INPUT_VIDEO,
                audio=path / INPUT_AUDIO,
                output=path / OUTPUT_MUX,
                container=request.container,
            )
        if request.op == "ffprobe_validate":
            assert self.ffprobe is not None
            return ffprobe_argv(ffprobe=self.ffprobe, source=path / OUTPUT_MUX)
        assert self.ytdlp is not None and self.proxy_url is not None
        assert request.url is not None and request.provider_id is not None
        cache = path / "cache"
        # Tool uid owns nested dirs; supervisor 0:0 cannot mkdir into 02770.
        if self._make_job is not None:
            make_owned_directory(cache, uid=TOOL_UID, gid=WORKER_GID, mode=0o700)
        elif not cache.exists():
            cache.mkdir(mode=0o700)
        if request.op == "inspect_metadata":
            return network_inspect_argv(
                executable=self.ytdlp,
                url=request.url,
                provider_id=request.provider_id,
                proxy_url=self.proxy_url,
                socket_timeout=self.socket_timeout_seconds,
                cache_dir=cache,
            )
        assert request.format_token is not None
        assert request.max_bytes is not None
        effective = min(request.max_bytes, self.max_filesize_bytes)
        return network_download_argv(
            executable=self.ytdlp,
            url=request.url,
            provider_id=request.provider_id,
            format_token=request.format_token,
            proxy_url=self.proxy_url,
            socket_timeout=self.socket_timeout_seconds,
            cache_dir=cache,
            output_template=str(path / "output-artifact.%(ext)s"),
            max_filesize_bytes=effective,
        )

    def serve(self, sock: socket.socket) -> None:
        sock.listen(4)
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            if not self._connections.acquire(blocking=False):
                _send(conn, dump_response({"ok": False, "code": "overflow"}))
                conn.close()
                continue
            threading.Thread(
                target=self._serve_one,
                args=(conn,),
                daemon=True,
            ).start()

    def _serve_one(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            raw = _read_frame(conn)
            uid = self._peer_lookup(conn)
            if len(raw) > MAX_REQUEST_BYTES:
                body = dump_response({"ok": False, "code": "oversized"})
            else:
                body = self.handle(raw, peer_uid=uid)
            conn.settimeout(5)
            _send(conn, body)
        except (OSError, ProtocolError, ValueError):
            try:
                _send(conn, dump_response({"ok": False, "code": "malformed"}))
            except OSError:
                return
        finally:
            conn.close()
            self._connections.release()


def peer_uid(conn: socket.socket) -> int:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OSError("SO_PEERCRED unavailable")
    raw = conn.getsockopt(socket.SOL_SOCKET, option, 12)
    _pid, uid, _gid = struct.unpack("3i", raw[:12])
    return int(uid)


def _read_frame(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_REQUEST_BYTES:
        block = conn.recv(4096)
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if b"\n" in block:
            break
    return b"".join(chunks)


def _send(conn: socket.socket, body: bytes) -> None:
    view = memoryview(body)
    while view:
        sent = conn.send(view)
        view = view[sent:]


def _find_artifact(job_dir: Path) -> Path | None:
    matches = sorted(job_dir.glob(OUTPUT_ARTIFACT_GLOB))
    files = [path for path in matches if path.is_file() and not path.is_symlink()]
    if len(files) != 1:
        return None
    return files[0]


def _find_artifact_as_worker(job_dir: Path) -> tuple[str, int] | None:
    """Stat artifact in a helper process — never mutate server thread credentials."""
    try:
        return artifact_stat(job_dir)
    except OSError:
        return None


def _outcome_payload(outcome: ToolOutcome) -> dict[str, object]:
    stdout = outcome.stdout[:_MAX_STDOUT]
    stderr = outcome.stderr[:_MAX_STDERR]
    code = "ok"
    if outcome.cancelled:
        code = "cancelled"
    elif outcome.timed_out:
        code = "timed_out"
    elif outcome.exit_code != 0:
        code = "failed"
    return {
        "ok": code == "ok",
        "code": code,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "cancelled": outcome.cancelled,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
    }
