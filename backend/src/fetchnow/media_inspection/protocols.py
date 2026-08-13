"""Protocols for media inspection extractors and process execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from fetchnow.media_inspection.models import ExtractedMediaDraft


@dataclass(frozen=True, slots=True, repr=False)
class InspectionTarget:
    """Validated provider target accepted by extractors.

    Built only from a consistent ResolutionResult validated snapshot — never
    from a raw user URL. ``tool_url`` is query/fragment-free.
    """

    provider_id: str
    hostname: str
    media_id: str
    # Query/fragment-stripped HTTPS provider URL derived from trusted identity.
    canonical_provider_url: str
    tool_url: str = field(repr=False)
    allowed_hostnames: frozenset[str] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "InspectionTarget("
            f"provider_id={self.provider_id!r}, "
            f"hostname={self.hostname!r}, "
            f"media_id={self.media_id!r}, "
            f"canonical_provider_url={self.canonical_provider_url!r})"
        )


class MediaExtractor(Protocol):
    """Provider-scoped metadata extractor (metadata-only)."""

    @property
    def extractor_id(self) -> str:
        """Stable extractor adapter id (token-shaped)."""

    async def extract(self, target: InspectionTarget) -> ExtractedMediaDraft:
        """Extract bounded metadata for a validated provider target."""


@dataclass(frozen=True, slots=True, repr=False)
class ProcessResult:
    """Bounded subprocess outcome — stdout/stderr are raw bytes, never logged."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cancelled: bool
    signal: int | None = None
    stdout_byte_count: int = 0
    stderr_byte_count: int = 0
    failure_class: str | None = None
    process_exit_category: str | None = None

    def __repr__(self) -> str:
        return (
            "ProcessResult("
            f"exit_code={self.exit_code!r}, "
            f"timed_out={self.timed_out!r}, "
            f"cancelled={self.cancelled!r}, "
            f"signal={self.signal!r}, "
            f"stdout_bytes={self.stdout_byte_count or len(self.stdout)}, "
            f"stderr_bytes={self.stderr_byte_count or len(self.stderr)}, "
            f"failure_class={self.failure_class!r}, "
            f"process_exit_category={self.process_exit_category!r})"
        )


class ProcessRunner(Protocol):
    """Executable runner used by hardened tool adapters (injectable for tests)."""

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
        """Execute argv with shell=False semantics and bounded pipes."""
