"""UDS supervisor. PID 1 owns cgroups; it cannot publish a download."""

from __future__ import annotations

import base64
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fetchnow.media_executor.argv import ffprobe_argv, mux_argv
from fetchnow.media_executor.constants import (
    INPUT_AUDIO,
    INPUT_VIDEO,
    MAX_JOBS,
    MAX_REQUEST_BYTES,
    OUTPUT_MUX,
    WORKER_UID,
)
from fetchnow.media_executor.layout import job_directory
from fetchnow.media_executor.protocol import (
    ProtocolError,
    Request,
    dump_response,
    parse_request,
)
from fetchnow.media_executor.runner import ToolOutcome, ToolRunner

_MAX_STDOUT = 196_608
_MAX_STDERR = 65_536
_OPERATION_SECONDS = 120.0
_CONNECT_LIMIT = 8


@dataclass
class _Job:
    state: str
    op: str | None = None
    container: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None


class ExecutorApp:
    def __init__(
        self,
        *,
        work_root: Path,
        runner: ToolRunner,
        ffmpeg: str,
        ffprobe: str,
        socket_dir: Path,
        make_job: Callable[[Path], None] | None = None,
        remove_job: Callable[[Path], None] | None = None,
        peer_lookup: Callable[[socket.socket], int] | None = None,
    ) -> None:
        self.work_root = work_root
        self.runner = runner
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
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
            request = parse_request(raw)
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
        if request.op == "mux_copy":
            return self._run_tool(request)
        if request.op == "ffprobe_validate":
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
                # Do not re-adopt a directory left by a previous executor epoch.
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
                and current.result is not None
            ):
                return current.result
            if running >= MAX_JOBS:
                return {"ok": False, "code": "overflow"}
            if current is None or current.state not in {"reserved", "done"}:
                return {"ok": False, "code": "not_reserved"}
            argv = self._argv(request, path)
            current.state = "running"
            current.op = request.op
            current.container = request.container
            current.cancel = threading.Event()
            cancel = current.cancel
        protected = [str(self.socket_dir), str(self.work_root)]
        try:
            outcome = self.runner.run(
                attempt=path,
                argv=argv,
                timeout_seconds=_OPERATION_SECONDS,
                cancel=cancel,
                protected=protected,
            )
        except Exception:
            with self._lock:
                job = self._jobs[request.key]
                job.state = "reserved"
                job.op = None
            raise
        payload = _outcome_payload(outcome)
        with self._lock:
            job = self._jobs[request.key]
            job.state = "done"
            job.result = payload
        return payload

    def _argv(self, request: Request, path: Path) -> list[str]:
        if request.op == "mux_copy":
            assert request.container is not None
            return mux_argv(
                ffmpeg=self.ffmpeg,
                video=path / INPUT_VIDEO,
                audio=path / INPUT_AUDIO,
                output=path / OUTPUT_MUX,
                container=request.container,
            )
        return ffprobe_argv(ffprobe=self.ffprobe, source=path / OUTPUT_MUX)

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
