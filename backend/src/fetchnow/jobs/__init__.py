"""Durable PostgreSQL media-inspection job orchestration (PR5).

Ownership protocol: client-generated 32-byte access token (base64url, 43 chars).
Only domain-separated SHA-256 digests are persisted. Raw tokens are never stored,
logged, or included in exception repr.
"""

from __future__ import annotations

from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
    parse_access_token,
    tokens_match,
)
from fetchnow.jobs.errors import JobError, JobErrorCode, raise_job_error
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.service import JobCreateView, JobView, MediaJobService
from fetchnow.jobs.states import MediaJobState, assert_transition
from fetchnow.jobs.worker_loop import MediaJobWorkerRunner

__all__ = [
    "JobCreateView",
    "JobError",
    "JobErrorCode",
    "JobView",
    "MediaJob",
    "MediaJobRepository",
    "MediaJobService",
    "MediaJobState",
    "MediaJobWorkerRunner",
    "assert_transition",
    "generate_access_token",
    "hash_access_token",
    "hash_request_fingerprint",
    "parse_access_token",
    "raise_job_error",
    "tokens_match",
]
