"""Worker-side client. A missing executor is a failure, not a local fallback."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path

from fetchnow.media_executor.constants import MAX_RESPONSE_BYTES, PROTOCOL_VERSION
from fetchnow.media_inspection.protocols import ProcessResult


class ExecutorCallError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class UnixExecutorClient:
    def __init__(self, socket_path: Path) -> None:
        self._path = socket_path

    async def reserve(self, *, job_id: str, attempt: int, fence: int) -> None:
        payload = await self._call(
            {"op": "reserve", "job_id": job_id, "attempt": attempt, "fence": fence}
        )
        if payload.get("ok") is not True or payload.get("code") != "reserved":
            raise ExecutorCallError("reserve_failed")

    async def mux_copy(
        self,
        *,
        job_id: str,
        attempt: int,
        fence: int,
        container: str,
        timeout_seconds: float,
    ) -> ProcessResult:
        payload = await self._call(
            {
                "op": "mux_copy",
                "job_id": job_id,
                "attempt": attempt,
                "fence": fence,
                "container": container,
            },
            timeout_seconds=timeout_seconds,
        )
        return _process_result(payload)

    async def ffprobe_validate(
        self,
        *,
        job_id: str,
        attempt: int,
        fence: int,
        timeout_seconds: float,
    ) -> ProcessResult:
        payload = await self._call(
            {
                "op": "ffprobe_validate",
                "job_id": job_id,
                "attempt": attempt,
                "fence": fence,
            },
            timeout_seconds=timeout_seconds,
        )
        return _process_result(payload)

    async def cancel(self, *, job_id: str, attempt: int, fence: int) -> None:
        await self._call(
            {"op": "cancel", "job_id": job_id, "attempt": attempt, "fence": fence},
            timeout_seconds=2,
        )

    async def release(self, *, job_id: str, attempt: int, fence: int) -> None:
        payload = await self._call(
            {"op": "release", "job_id": job_id, "attempt": attempt, "fence": fence},
            timeout_seconds=5,
        )
        if payload.get("ok") is not True:
            code = payload.get("code")
            raise ExecutorCallError(code if isinstance(code, str) else "protocol")

    async def inspect_metadata(
        self,
        *,
        job_id: str,
        attempt: int,
        fence: int,
        url: str,
        provider_id: str,
        timeout_seconds: float,
    ) -> ProcessResult:
        payload = await self._call(
            {
                "op": "inspect_metadata",
                "job_id": job_id,
                "attempt": attempt,
                "fence": fence,
                "url": url,
                "provider_id": provider_id,
            },
            timeout_seconds=timeout_seconds,
        )
        return _process_result(payload)

    async def download(
        self,
        *,
        op: str,
        job_id: str,
        attempt: int,
        fence: int,
        url: str,
        provider_id: str,
        format_token: str,
        timeout_seconds: float,
        max_bytes: int,
        min_free_bytes: int,
    ) -> dict[str, object]:
        if op not in {
            "download_progressive",
            "download_video",
            "download_audio",
        }:
            raise ExecutorCallError("protocol")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ExecutorCallError("protocol")
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise ExecutorCallError("protocol")
        payload = await self._call(
            {
                "op": op,
                "job_id": job_id,
                "attempt": attempt,
                "fence": fence,
                "url": url,
                "provider_id": provider_id,
                "format_token": format_token,
                "max_bytes": max_bytes,
                "min_free_bytes": min_free_bytes,
            },
            timeout_seconds=timeout_seconds,
        )
        if payload.get("ok") is not True:
            # Still return payload so callers can map timeout/cancel codes.
            result = _process_result(payload)
            return {"result": result, "payload": payload}
        result = _process_result(payload)
        name = payload.get("artifact_name")
        size = payload.get("artifact_bytes")
        if not isinstance(name, str) or type(size) is not int or size < 0:
            raise ExecutorCallError("protocol")
        if "/" in name or name.startswith(".") or ".." in name:
            raise ExecutorCallError("protocol")
        return {
            "result": result,
            "payload": payload,
            "artifact_name": name,
            "artifact_bytes": size,
        }

    async def _call(
        self,
        fields: dict[str, object],
        *,
        timeout_seconds: float = 5,
    ) -> dict[str, object]:
        body = dict(fields)
        body["v"] = PROTOCOL_VERSION
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
        # One connection per call: cancel must not wait behind the running RPC.
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._path), limit=MAX_RESPONSE_BYTES),
                timeout=2,
            )
        except (TimeoutError, OSError) as exc:
            raise ExecutorCallError("unavailable") from exc
        try:
            writer.write(raw)
            await asyncio.wait_for(writer.drain(), timeout=2)
            framed = await asyncio.wait_for(
                reader.readuntil(b"\n"),
                timeout=timeout_seconds,
            )
        except (
            TimeoutError,
            OSError,
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
        ) as exc:
            raise ExecutorCallError("protocol") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        if len(framed) > MAX_RESPONSE_BYTES:
            raise ExecutorCallError("protocol")
        try:
            payload = json.loads(framed.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExecutorCallError("protocol") from exc
        if not isinstance(payload, dict) or payload.get("v") != PROTOCOL_VERSION:
            raise ExecutorCallError("protocol")
        code = payload.get("code")
        if code in {"unauthorized", "malformed", "oversized", "unknown_operation"}:
            raise ExecutorCallError("protocol")
        if code == "unavailable":
            raise ExecutorCallError("unavailable")
        return payload


def _process_result(payload: dict[str, object]) -> ProcessResult:
    try:
        stdout = base64.b64decode(str(payload.get("stdout_b64", "")), validate=True)
        stderr = base64.b64decode(str(payload.get("stderr_b64", "")), validate=True)
    except (ValueError, TypeError) as exc:
        raise ExecutorCallError("protocol") from exc
    exit_code = payload.get("exit_code")
    if exit_code is not None and type(exit_code) is not int:
        raise ExecutorCallError("protocol")
    resolved: int | None = exit_code if isinstance(exit_code, int) else None
    if (
        payload.get("ok") is not True
        and resolved is None
        and not payload.get("timed_out")
        and not payload.get("cancelled")
    ):
        resolved = 1
    return ProcessResult(
        exit_code=resolved,
        stdout=stdout,
        stderr=stderr,
        timed_out=bool(payload.get("timed_out")),
        cancelled=bool(payload.get("cancelled")),
        stdout_byte_count=len(stdout),
        stderr_byte_count=len(stderr),
    )
