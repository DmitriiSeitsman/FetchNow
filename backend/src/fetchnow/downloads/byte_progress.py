"""Byte-aware download percentage from observed workspace bytes.

The backend never parses yt-dlp human-readable stdout. Percentage exists only
when a bounded denominator is known. 100 is never stored: verifying / muxing /
publishing still follow the download subprocess.

Observed percent and persisted percent are distinct. A throttled or failed
sample must not be treated as written.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

DOWNLOAD_PERCENT_STAGES = frozenset({"downloading_video", "downloading_audio"})
PROGRESS_PERCENT_MIN = 0
PROGRESS_PERCENT_MAX = 99
PROGRESS_WRITE_INTERVAL_SECONDS = 0.5
PROGRESS_BACKOFF_INITIAL_SECONDS = 0.5
PROGRESS_BACKOFF_MAX_SECONDS = 8.0


def progress_percent(
    *,
    observed_bytes: int,
    expected_bytes: int | None,
    previous: int | None = None,
) -> int | None:
    """floor(observed / expected × 100), clamp 0..99, monotonic within a stage."""
    if expected_bytes is None or type(expected_bytes) is bool:
        return None
    if not isinstance(expected_bytes, int) or expected_bytes <= 0:
        return None
    if type(observed_bytes) is bool or not isinstance(observed_bytes, int):
        return None
    observed = max(0, observed_bytes)
    raw = (observed * 100) // expected_bytes
    bounded = min(PROGRESS_PERCENT_MAX, max(PROGRESS_PERCENT_MIN, raw))
    if previous is None:
        return bounded
    if type(previous) is bool or not isinstance(previous, int):
        return bounded
    return max(previous, bounded)


def should_persist_percent(
    *,
    candidate: int | None,
    stored: int | None,
    elapsed_seconds: float,
    min_interval_seconds: float = PROGRESS_WRITE_INTERVAL_SECONDS,
) -> bool:
    """Throttle: write only when the integer percent changed and interval elapsed.

    ``stored`` is the last **successfully persisted** percent, never a
    throttled observation. Initial ``stored is None`` (including percent 0)
    may write immediately; later 1..99 still require the interval.
    """
    if candidate is None:
        return False
    if stored is not None and candidate <= stored:
        return False
    if stored is None:
        return True
    return elapsed_seconds >= min_interval_seconds


class ProgressPersistGate:
    """Split max observed percent from last confirmed persisted percent.

    Monotonic calculation uses ``max_observed_percent``. Persist decisions
    compare the candidate only with ``last_persisted_percent``. That stored
    value changes only after a confirmed database write. Throttled samples
    and failed attempts are not persisted. Transient failures use bounded
    exponential backoff so a 0.2s sampler cannot hammer the database.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval_seconds: float = PROGRESS_WRITE_INTERVAL_SECONDS,
        backoff_initial_seconds: float = PROGRESS_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = PROGRESS_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._clock = clock
        self._min_interval = min_interval_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._backoff = backoff_initial_seconds
        self.max_observed_percent: int | None = None
        self.last_persisted_percent: int | None = None
        self.last_persist_attempt_at: float | None = None
        self.last_successful_write_at: float | None = None
        self.ownership_lost = False
        self.observed_sample_count = 0
        self.persisted_update_count = 0

    def note_observed(self, candidate: int | None) -> int | None:
        """Update max observed percent. Return the value to persist, or None."""
        if self.ownership_lost or candidate is None:
            return None
        if self.max_observed_percent is None:
            self.max_observed_percent = candidate
        else:
            self.max_observed_percent = max(self.max_observed_percent, candidate)
        if not self._should_attempt_persist():
            return None
        return self.max_observed_percent

    def mark_persisted(self, percent: int) -> None:
        now = self._clock()
        self.last_persisted_percent = percent
        self.last_successful_write_at = now
        self.last_persist_attempt_at = now
        self._backoff = self._backoff_initial
        self.persisted_update_count += 1

    def mark_attempt_failed(self) -> None:
        self.last_persist_attempt_at = self._clock()
        doubled = max(self._backoff, self._backoff_initial) * 2
        self._backoff = min(doubled, self._backoff_max)

    def mark_ownership_lost(self) -> None:
        self.ownership_lost = True

    def diagnostic_snapshot(self, *, expected_size_known: bool) -> dict[str, object]:
        """Safe aggregates for end-of-stage logging (no paths/URLs/tokens)."""
        return {
            "observed_sample_count": self.observed_sample_count,
            "persisted_update_count": self.persisted_update_count,
            "max_persisted_percent": self.last_persisted_percent,
            "expected_size_known": bool(expected_size_known),
        }

    async def ingest(
        self,
        *,
        observed_bytes: int,
        expected_bytes: int | None,
        persist: Callable[[int], Awaitable[bool]],
        force: bool = False,
    ) -> None:
        """Observe workspace bytes and persist only when the gate allows it.

        ``persist`` must return True only after a confirmed fenced write.
        False (stale fence / cancel / expired lease) stops further percent
        writes. Exceptions are treated as transient failures with backoff.

        ``force=True`` bypasses throttle/backoff once for an end-of-stage
        final sample (still monotonic and ownership-checked).
        """
        if self.ownership_lost:
            return
        self.observed_sample_count += 1
        candidate = progress_percent(
            observed_bytes=observed_bytes,
            expected_bytes=expected_bytes,
            previous=self.max_observed_percent,
        )
        if force:
            if candidate is None:
                return
            if self.max_observed_percent is None:
                to_write = candidate
            else:
                to_write = max(self.max_observed_percent, candidate)
            self.max_observed_percent = to_write
            if (
                self.last_persisted_percent is not None
                and to_write <= self.last_persisted_percent
            ):
                return
        else:
            maybe_write = self.note_observed(candidate)
            if maybe_write is None:
                return
            to_write = maybe_write
        try:
            applied = await persist(to_write)
        except Exception:
            self.mark_attempt_failed()
            raise
        if applied:
            self.mark_persisted(to_write)
            return
        self.mark_ownership_lost()

    def _should_attempt_persist(self) -> bool:
        if self.ownership_lost:
            return False
        if not should_persist_percent(
            candidate=self.max_observed_percent,
            stored=self.last_persisted_percent,
            elapsed_seconds=self._throttle_elapsed(),
            min_interval_seconds=self._min_interval,
        ):
            return False
        return not self._in_backoff()

    def _throttle_elapsed(self) -> float:
        if self.last_successful_write_at is None:
            return self._min_interval
        return self._clock() - self.last_successful_write_at

    def _in_backoff(self) -> bool:
        if self.last_persist_attempt_at is None:
            return False
        if (
            self.last_successful_write_at is not None
            and self.last_persist_attempt_at <= self.last_successful_write_at
        ):
            return False
        return (self._clock() - self.last_persist_attempt_at) < self._backoff
