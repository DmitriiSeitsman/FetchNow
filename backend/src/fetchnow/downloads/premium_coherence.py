"""Shared READY Free→Premium coherence checks for expiry and reconcile."""

from __future__ import annotations

import uuid

from fetchnow.downloads.errors import DownloadError
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.snapshot_codec import (
    decode_authorized_identity_id,
    decode_effective_policy_snapshot,
)


def is_coherent_promoted_premium_job(
    job: MediaDownloadJob,
    *,
    expected_identity_id: uuid.UUID | None = None,
) -> bool:
    """Return True when the stored snapshot is a valid promoted Premium policy.

    Requires a successful policy decode (not a raw ``tier`` string match), a
    Premium tier, and a bound ``authorizedIdentityId``. When
    ``expected_identity_id`` is provided it must match that bound identity.
    Malformed snapshots and Free/legacy policies return False.
    """
    try:
        policy = decode_effective_policy_snapshot(job.selected_format_snapshot)
        identity = decode_authorized_identity_id(job.selected_format_snapshot)
    except DownloadError:
        return False
    except Exception:
        return False
    if policy is None or policy.tier != "premium" or identity is None:
        return False
    return expected_identity_id is None or identity == expected_identity_id
