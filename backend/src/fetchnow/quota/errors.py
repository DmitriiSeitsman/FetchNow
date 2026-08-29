"""Public-safe quota domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    tier: str
    download_limit: int
    downloads_used: int
    downloads_reserved: int
    downloads_remaining: int
    reset_at: datetime | None


class AnonymousIdentityRequiredError(Exception):
    """A quota-governed admission lacked a valid bootstrapped identity."""

    code = "FREE_QUOTA_IDENTITY_REQUIRED"
    message = "A valid anonymous client identity is required."
    http_status = 428


class FreeQuotaExceededError(Exception):
    """The effective Free rolling-window quota has no admission capacity."""

    code = "FREE_DOWNLOAD_QUOTA_EXHAUSTED"
    message = "The Free download quota is exhausted."
    http_status = 429

    def __init__(self, status: QuotaStatus) -> None:
        self.status = status
        super().__init__(self.message)


class QuotaInvariantError(RuntimeError):
    """Internal fail-closed quota persistence invariant violation."""
