"""HTTP live/ready probes (loopback only for staging)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import (
    HEALTH_HTTP_TIMEOUT_SECONDS,
    HEALTH_MAX_BODY_BYTES,
    HEALTH_OVERALL_DEADLINE_SECONDS,
    HEALTH_RETRY_DELAY_SECONDS,
)


class HttpHealthError(ValueError):
    """HTTP health probe failure."""


@dataclass(frozen=True)
class HttpCheckResult:
    path: str
    attempts: int
    status: int | None
    ok: bool
    detail: str


def _fetch_json(url: str, *, timeout: float) -> tuple[int, dict[str, Any]]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            raise HttpHealthError(f"unexpected redirect HTTP {code} -> {newurl}")

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            raw = resp.read(HEALTH_MAX_BODY_BYTES + 1)
            if len(raw) > HEALTH_MAX_BODY_BYTES:
                raise HttpHealthError("response body too large")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise HttpHealthError(f"invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise HttpHealthError("JSON payload must be an object")
            return int(status), payload
    except HttpHealthError:
        raise
    except urllib.error.HTTPError as exc:
        raise HttpHealthError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HttpHealthError(f"connection error: {exc.reason}") from exc


def check_endpoint(
    base_url: str,
    path: str,
    *,
    timeout: float = HEALTH_HTTP_TIMEOUT_SECONDS,
    deadline_seconds: float = HEALTH_OVERALL_DEADLINE_SECONDS,
    retry_delay: float = HEALTH_RETRY_DELAY_SECONDS,
) -> HttpCheckResult:
    if not base_url.startswith("http://127.0.0.1:") and not base_url.startswith(
        "http://localhost:"
    ):
        raise HttpHealthError("staging health base URL must be loopback only")
    url = base_url.rstrip("/") + path
    deadline = time.monotonic() + deadline_seconds
    attempts = 0
    last_detail = "not attempted"
    while time.monotonic() < deadline:
        attempts += 1
        try:
            status, payload = _fetch_json(url, timeout=timeout)
            if status != 200:
                last_detail = f"status={status}"
            elif payload.get("status") != "ok":
                last_detail = "JSON status field is not 'ok'"
            else:
                return HttpCheckResult(path, attempts, status, True, "ok")
        except HttpHealthError as exc:
            last_detail = str(exc)
        time.sleep(retry_delay)
    return HttpCheckResult(path, attempts, None, False, last_detail)
