"""Gateway routing convergence and public HTTPS health gates.

Loopback routing probes reuse the existing loopback-only HTTP helper.
Public HTTPS uses a separate strict origin contract and must never weaken
``http_health`` loopback validation.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import (
    HEALTH_HTTP_TIMEOUT_SECONDS,
    HEALTH_MAX_BODY_BYTES,
    HEALTH_RETRY_DELAY_SECONDS,
)
from .environment import EnvironmentIdentity, real_identity
from .http_health import HttpCheckResult, HttpHealthError, check_endpoint
from .redact import redact

# Bounded wait after upstream IP change (api/web ``resolve`` + Docker DNS).
# This is a release-gate deadline, not a zero-downtime promise.
ROUTING_CONVERGENCE_DEADLINE_SECONDS = 30.0
ROUTING_POLL_SECONDS = 1.0

PUBLIC_HTTPS_CONNECT_TIMEOUT_SECONDS = 5.0
PUBLIC_HTTPS_READ_TIMEOUT_SECONDS = 10.0
PUBLIC_HTTPS_OVERALL_DEADLINE_SECONDS = 30.0
PUBLIC_HTTPS_RETRY_DELAY_SECONDS = 1.0
PUBLIC_HTTPS_MAX_BODY_BYTES = HEALTH_MAX_BODY_BYTES

# Stable homepage marker from web/src/pages/index.astro (brand lockup).
HOMEPAGE_MARKER = 'class="brand">FetchNow</p>'

PUBLIC_LIVE_PATH = "/api/v1/health/live"
PUBLIC_READY_PATH = "/api/v1/health/ready"
PUBLIC_HOME_PATH = "/"

APPROVED_PUBLIC_ORIGINS: frozenset[str] = frozenset(
    {
        "https://staging.fetchnow.online",
        "https://fetchnow.online",
    }
)


class RoutingHealthError(ValueError):
    """Gateway routing or public HTTPS gate failure."""


@dataclass(frozen=True)
class PublicHttpsGateConfig:
    """Strict public HTTPS probe configuration.

    Production/staging CLI paths must use an approved origin with the default
    system trust store (``ssl_context is None``). Disposable TLS fixtures for
    local integration inject a custom ``ssl_context`` and a non-approved origin
    only through this dataclass — never via production CLI flags.
    """

    origin: str
    ssl_context: ssl.SSLContext | None = None
    allow_test_origin: bool = False


@dataclass(frozen=True)
class PublicHttpsCheckResult:
    path: str
    attempts: int
    status: int | None
    ok: bool
    detail: str


@dataclass(frozen=True)
class RoutingHealthResult:
    ok: bool
    messages: tuple[str, ...]
    loopback: tuple[HttpCheckResult, ...] = ()
    public: tuple[PublicHttpsCheckResult, ...] = ()


def public_https_config_for_real_project(project_name: str) -> PublicHttpsGateConfig:
    """Resolve the fixed approved origin for a real environment project."""
    identity = real_identity(project_name)
    if identity is None:
        raise RoutingHealthError(
            "public HTTPS origin is only defined for real staging/production projects"
        )
    return public_https_config_from_identity(identity)


def public_https_config_from_identity(
    identity: EnvironmentIdentity,
) -> PublicHttpsGateConfig:
    origin = identity.public_site_url.rstrip("/")
    if origin not in APPROVED_PUBLIC_ORIGINS:
        raise RoutingHealthError("identity public_site_url is not an approved origin")
    return PublicHttpsGateConfig(origin=origin)


def validate_public_https_origin(
    origin: str,
    *,
    allow_test_origin: bool = False,
) -> str:
    """Fail closed on scheme/host/port/userinfo/path/query/fragment abuse."""
    raw = origin.strip()
    if not raw:
        raise RoutingHealthError("public HTTPS origin is empty")
    if any(ch.isspace() for ch in raw):
        raise RoutingHealthError("public HTTPS origin must not contain whitespace")
    if "\\" in raw:
        raise RoutingHealthError("public HTTPS origin must not contain backslashes")

    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise RoutingHealthError("public HTTPS origin must use https")
    if (
        parts.username is not None
        or parts.password is not None
        or "@" in raw.split("://", 1)[-1].split("/", 1)[0]
    ):
        raise RoutingHealthError("public HTTPS origin must not include userinfo")
    if parts.path not in {"", "/"}:
        raise RoutingHealthError("public HTTPS origin must not include a path")
    if parts.query or parts.fragment:
        raise RoutingHealthError(
            "public HTTPS origin must not include query or fragment"
        )
    if not parts.hostname:
        raise RoutingHealthError("public HTTPS origin hostname is required")
    host = parts.hostname.lower()
    if host != parts.hostname:
        # urlsplit already lowercases host in many cases; normalize comparison.
        pass
    try:
        ipaddress.ip_address(host)
        raise RoutingHealthError("public HTTPS origin must not be a raw IP address")
    except ValueError:
        pass
    if parts.port is not None:
        if not allow_test_origin:
            raise RoutingHealthError(
                "public HTTPS origin must use the default HTTPS port"
            )
        if parts.port < 1024 or parts.port > 65535:
            raise RoutingHealthError(
                "test-only public HTTPS origin must use an unprivileged port"
            )

    if allow_test_origin:
        if not host.endswith(".test") and host != "localhost":
            raise RoutingHealthError(
                "test-only public HTTPS origin host must be localhost or *.test"
            )
        if parts.port is None:
            return f"https://{host}"
        return f"https://{host}:{parts.port}"

    normalized = f"https://{host}"
    if normalized not in APPROVED_PUBLIC_ORIGINS:
        raise RoutingHealthError("public HTTPS origin is not on the approved allowlist")
    return normalized


def _sanitize_public_error(exc: BaseException) -> str:
    return redact(str(exc))[:200]


def _public_opener(ssl_context: ssl.SSLContext | None) -> urllib.request.OpenerDirector:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            raise RoutingHealthError(f"unexpected redirect HTTP {code}")

    https_handler = urllib.request.HTTPSHandler(context=ssl_context)
    return urllib.request.build_opener(_NoRedirect, https_handler)


def _fetch_public(
    url: str,
    *,
    timeout: float,
    ssl_context: ssl.SSLContext | None,
    max_body: int,
    expect_json: bool,
) -> tuple[int, bytes]:
    opener = _public_opener(ssl_context)
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 200))
            raw = resp.read(max_body + 1)
            if len(raw) > max_body:
                raise RoutingHealthError("response body too large")
            if expect_json:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RoutingHealthError(f"invalid JSON: {exc}") from exc
                if not isinstance(payload, dict):
                    raise RoutingHealthError("JSON payload must be an object")
                if payload.get("status") != "ok":
                    raise RoutingHealthError("JSON status field is not 'ok'")
            return status, raw
    except RoutingHealthError:
        raise
    except ssl.SSLError as exc:
        raise RoutingHealthError(
            f"TLS verification failed: {_sanitize_public_error(exc)}"
        ) from exc
    except urllib.error.HTTPError as exc:
        # Do not follow redirects; HTTPError covers 3xx when redirect handler raises?
        # With NoRedirect, 3xx may surface as HTTPError depending on urllib version.
        if 300 <= int(exc.code) < 400:
            raise RoutingHealthError(f"unexpected redirect HTTP {exc.code}") from exc
        raise RoutingHealthError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise RoutingHealthError(
                f"TLS verification failed: {_sanitize_public_error(reason)}"
            ) from exc
        raise RoutingHealthError(
            f"connection error: {_sanitize_public_error(reason)}"
        ) from exc
    except (ConnectionError, TimeoutError, OSError) as exc:
        raise RoutingHealthError(
            f"connection error: {_sanitize_public_error(exc)}"
        ) from exc


def check_public_https_path(
    config: PublicHttpsGateConfig,
    path: str,
    *,
    expect_json: bool,
    require_homepage_marker: bool = False,
    connect_timeout: float = PUBLIC_HTTPS_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = PUBLIC_HTTPS_READ_TIMEOUT_SECONDS,
    deadline_seconds: float = PUBLIC_HTTPS_OVERALL_DEADLINE_SECONDS,
    retry_delay: float = PUBLIC_HTTPS_RETRY_DELAY_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> PublicHttpsCheckResult:
    origin = validate_public_https_origin(
        config.origin, allow_test_origin=config.allow_test_origin
    )
    if path != PUBLIC_HOME_PATH and (
        not path.startswith("/") or "?" in path or "#" in path
    ):
        raise RoutingHealthError("public HTTPS path is invalid")
    url = origin + path
    deadline = clock() + deadline_seconds
    attempts = 0
    last_detail = "not attempted"
    # urllib.urlopen accepts a single overall timeout (seconds), not a
    # (connect, read) tuple — passing a tuple raises TypeError on settimeout.
    timeout = float(connect_timeout) + float(read_timeout)
    while clock() < deadline:
        attempts += 1
        try:
            status, raw = _fetch_public(
                url,
                timeout=timeout,
                ssl_context=config.ssl_context,
                max_body=PUBLIC_HTTPS_MAX_BODY_BYTES,
                expect_json=expect_json,
            )
            if status != 200:
                last_detail = f"status={status}"
                if not is_transient_routing_failure(last_detail):
                    return PublicHttpsCheckResult(
                        path, attempts, status, False, last_detail
                    )
            elif require_homepage_marker:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RoutingHealthError("homepage is not valid UTF-8") from exc
                if HOMEPAGE_MARKER not in text:
                    last_detail = "homepage marker missing"
                    # Marker mismatch is a permanent content failure, not churn.
                    return PublicHttpsCheckResult(
                        path, attempts, status, False, last_detail
                    )
                return PublicHttpsCheckResult(path, attempts, status, True, "ok")
            else:
                return PublicHttpsCheckResult(path, attempts, status, True, "ok")
        except RoutingHealthError as exc:
            last_detail = str(exc)
            if not is_transient_routing_failure(last_detail):
                return PublicHttpsCheckResult(
                    path, attempts, None, False, last_detail
                )
        sleeper(retry_delay)
    return PublicHttpsCheckResult(path, attempts, None, False, last_detail)


def check_public_https_gate(
    config: PublicHttpsGateConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> RoutingHealthResult:
    """Probe approved public origin: homepage marker + live + ready."""
    checks: list[PublicHttpsCheckResult] = []
    messages: list[str] = []
    home = check_public_https_path(
        config,
        PUBLIC_HOME_PATH,
        expect_json=False,
        require_homepage_marker=True,
        clock=clock,
        sleeper=sleeper,
    )
    checks.append(home)
    if not home.ok:
        messages.append(f"FAIL: public HTTPS {PUBLIC_HOME_PATH}: {home.detail}")
        return RoutingHealthResult(False, tuple(messages), public=tuple(checks))

    for path in (PUBLIC_LIVE_PATH, PUBLIC_READY_PATH):
        result = check_public_https_path(
            config,
            path,
            expect_json=True,
            clock=clock,
            sleeper=sleeper,
        )
        checks.append(result)
        if not result.ok:
            messages.append(f"FAIL: public HTTPS {path}: {result.detail}")
            return RoutingHealthResult(False, tuple(messages), public=tuple(checks))

    messages.append("OK: public HTTPS gate passed")
    return RoutingHealthResult(True, tuple(messages), public=tuple(checks))


def wait_gateway_routing_ready(
    gateway_base_url: str,
    *,
    deadline_seconds: float = ROUTING_CONVERGENCE_DEADLINE_SECONDS,
    poll_seconds: float = ROUTING_POLL_SECONDS,
    http_timeout: float = HEALTH_HTTP_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> RoutingHealthResult:
    """Poll loopback gateway routes until live+ready succeed within the deadline.

    Intended after api/web recreate while the gateway process/container stays
    unchanged. Transient 502/connection errors are retried until the deadline.
    """
    deadline = clock() + deadline_seconds
    attempts = 0
    last_detail = "not attempted"
    loopback: list[HttpCheckResult] = []
    while clock() < deadline:
        attempts += 1
        remaining = max(0.1, deadline - clock())
        try:
            live = check_endpoint(
                gateway_base_url,
                PUBLIC_LIVE_PATH,
                timeout=min(http_timeout, remaining),
                deadline_seconds=min(http_timeout + 0.5, remaining),
                retry_delay=HEALTH_RETRY_DELAY_SECONDS,
            )
            loopback.append(live)
            if not live.ok:
                last_detail = f"live: {live.detail}"
                if not is_transient_routing_failure(live.detail):
                    return RoutingHealthResult(
                        False,
                        (f"FAIL: gateway routing permanent failure: {last_detail}",),
                        loopback=tuple(loopback),
                    )
                sleeper(poll_seconds)
                continue
            ready = check_endpoint(
                gateway_base_url,
                PUBLIC_READY_PATH,
                timeout=min(http_timeout, max(0.1, deadline - clock())),
                deadline_seconds=min(http_timeout + 0.5, max(0.1, deadline - clock())),
                retry_delay=HEALTH_RETRY_DELAY_SECONDS,
            )
            loopback.append(ready)
            if not ready.ok:
                last_detail = f"ready: {ready.detail}"
                if not is_transient_routing_failure(ready.detail):
                    return RoutingHealthResult(
                        False,
                        (f"FAIL: gateway routing permanent failure: {last_detail}",),
                        loopback=tuple(loopback),
                    )
                sleeper(poll_seconds)
                continue
            return RoutingHealthResult(
                True,
                (
                    f"OK: gateway routing ready after {attempts} attempt(s) "
                    f"within {deadline_seconds:.0f}s",
                ),
                loopback=tuple(loopback),
            )
        except (HttpHealthError, ConnectionError, TimeoutError, OSError) as exc:
            last_detail = str(exc)
            if not is_transient_routing_failure(last_detail):
                return RoutingHealthResult(
                    False,
                    (f"FAIL: gateway routing permanent failure: {last_detail}",),
                    loopback=tuple(loopback),
                )
            sleeper(poll_seconds)
    return RoutingHealthResult(
        False,
        (
            f"FAIL: gateway routing did not converge within "
            f"{deadline_seconds:.0f}s: {last_detail}",
        ),
        loopback=tuple(loopback),
    )


def is_transient_routing_failure(detail: str) -> bool:
    """Classify probe failures that may clear after DNS/upstream convergence."""
    text = detail.lower()
    transient_markers = (
        "connection error",
        "timeout",
        "status=502",
        "status=503",
        "status=504",
        "http 502",
        "http 503",
        "http 504",
        "temporarily",
        "name or service not known",
        "nodename nor servname",
    )
    return any(marker in text for marker in transient_markers)
