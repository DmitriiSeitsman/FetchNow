"""Shared helpers for media inspection tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fetchnow.core.config import Settings
from fetchnow.media_inspection.protocols import ProcessResult
from fetchnow.resolution.models import ResolutionProvenance, ResolutionResult
from fetchnow.url.models import NormalizedMediaURL, URLValidationResult
from fetchnow.url.providers import ProviderRegistry


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
        "PROVIDER_OK_ENABLED": True,
        "MEDIA_INSPECTION_ENABLED": True,
        "MEDIA_INSPECTION_YTDLP_PATH": "/usr/bin/true",
        "MEDIA_INSPECTION_TIMEOUT_SECONDS": 5,
        "MEDIA_INSPECTION_SOCKET_TIMEOUT_SECONDS": 2,
        "MEDIA_INSPECTION_STDOUT_MAX_BYTES": 65536,
        "MEDIA_INSPECTION_STDERR_MAX_BYTES": 16384,
        "MEDIA_INSPECTION_MAX_FORMATS": 32,
        "MEDIA_INSPECTION_MAX_TITLE_LENGTH": 80,
        "MAX_SOURCE_DURATION_SECONDS": 3600,
        "MAX_SOURCE_FILE_BYTES": 536870912,
        "MEDIA_DOWNLOAD_MAX_BYTES": 536870912,
        "TEMP_STORAGE_PATH": "/tmp/fetchnow-test-tmp-unused",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_regular_executable(tmp_path: Path, name: str = "fake-ytdlp") -> str:
    """Create a non-symlink executable regular file for adapter path checks."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    assert not path.is_symlink()
    return str(path)


def make_temp_root(tmp_path: Path, name: str = "inspect-root") -> str:
    """Private non-world-writable absolute temp root for inspection."""
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return str(root)


def providers(s: Settings | None = None) -> ProviderRegistry:
    return ProviderRegistry.from_settings(s or settings())


def resolution(
    *,
    provider_id: str,
    canonical: str,
    hostname: str | None = None,
    path: str = "/",
    query: str = "",
    provenance: ResolutionProvenance = ResolutionProvenance.DIRECT_PROVIDER,
    validated_provider_id: str | None = None,
    validated_hostname: str | None = None,
    validated_canonical: str | None = None,
    validated_path: str | None = None,
    scheme: str = "https",
    original_scheme: str | None = None,
    port: int | None = None,
    public_canonical: str | None = None,
) -> ResolutionResult:
    host = hostname or canonical.split("://", 1)[1].split("/", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    v_host = validated_hostname or host
    v_provider = validated_provider_id or provider_id
    v_canonical = validated_canonical or canonical
    v_path = validated_path if validated_path is not None else path
    normalized = NormalizedMediaURL(
        original_scheme=original_scheme or scheme,
        scheme=scheme,
        hostname=v_host,
        port=port,
        path=v_path,
        query=query,
        provider_id=v_provider,
        canonical=v_canonical,
    )
    validated = URLValidationResult(
        provider_id=v_provider,
        provider_display_name=v_provider.upper(),
        url=normalized,
    )
    return ResolutionResult(
        submitted_url=canonical,
        validated=validated,
        provider_id=provider_id,
        provider_display_name=provider_id.upper(),
        canonical_provider_url=(
            public_canonical if public_canonical is not None else canonical
        ),
        wrapper_type=None,
        provenance=provenance,
        resolution_chain=(),
    )


@dataclass
class FakeRunner:
    """Records argv/env and returns a scripted ProcessResult."""

    result: ProcessResult
    calls: list[dict[str, Any]]
    raise_exc: BaseException | None

    def __init__(
        self,
        result: ProcessResult | None = None,
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.result = result or ProcessResult(
            exit_code=0,
            stdout=b"{}",
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
        self.calls = []
        self.shell_used = False
        self.raise_exc = raise_exc

    async def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": list(argv),
                "env": dict(env),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "stdout_limit_bytes": stdout_limit_bytes,
                "stderr_limit_bytes": stderr_limit_bytes,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result
