"""Wrapper resolver protocol and document-fetch port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fetchnow.network.models import SafeDocumentResponse, SafeDocumentTarget
from fetchnow.resolution.models import WrapperResolveOutcome

# Domain alias kept for resolver authors.
WrapperValidatedURL = SafeDocumentTarget


@runtime_checkable
class DocumentFetchPort(Protocol):
    """Resolver-scoped GET document fetch.

    Allowed hosts are fixed by the orchestration layer when the port is
    created. Resolvers cannot pass or override an allowlist.
    """

    async def fetch_document(
        self,
        validated: SafeDocumentTarget,
    ) -> SafeDocumentResponse:
        """Fetch a bounded HTML/JSON document within the scoped host set."""


@runtime_checkable
class WrapperResolver(Protocol):
    """Single wrapper type → candidate URL extractor.

    Resolvers must not recurse, must not open unrestricted HTTP clients, and
    must not declare terminal media providers.
    """

    @property
    def resolver_id(self) -> str:
        """Stable unique resolver identity."""

    @property
    def wrapper_type(self) -> str:
        """Stable wrapper-type identity (one resolver per type)."""

    @property
    def exact_hostnames(self) -> frozenset[str]:
        """Exact hostnames this resolver owns (label-boundary safe)."""

    def matches(self, validated: SafeDocumentTarget) -> bool:
        """Return True when this resolver owns the validated wrapper URL."""

    async def resolve(
        self,
        validated: SafeDocumentTarget,
        *,
        documents: DocumentFetchPort,
    ) -> WrapperResolveOutcome:
        """Extract the next candidate URL (provider or nested wrapper)."""
