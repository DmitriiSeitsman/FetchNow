"""N11: real worker watchdog + native RPC, with an injected lost-lease answer.

No database is used here. This is not a DB transaction/READY integration proof.
The controlling worker must reject the operation and cancel its native tree.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import network_acceptance as harness

from fetchnow.downloads.executor import (
    DownloadClaimSnapshot,
    DownloadExecutor,
    _LeaseLostError,
)
from fetchnow.media_executor.client import ExecutorCallError, _process_result


async def prove(project: str) -> dict[str, object]:
    job, fence = str(uuid.uuid4()), 7
    reserved = await asyncio.to_thread(
        harness._net_rpc, project, harness._request(job, fence, "reserve")
    )
    if reserved.get("code") != "reserved":
        raise OSError("native reservation failed")
    worker = DownloadExecutor.__new__(DownloadExecutor)
    worker._lease_lost = False
    worker._user_cancel = False
    worker._watchdog_tasks = set()
    worker._watchdog_started = worker._watchdog_finished = 0
    observations: dict[str, object] = {}

    async def wait() -> None:
        await asyncio.sleep(0.02)

    async def lost_lease(*, job_id: uuid.UUID, fence: int) -> bool:
        assert str(job_id) == job and fence == 7
        observations["before"] = await asyncio.to_thread(
            harness._wait_fixture_started, project, job, fence
        )
        return False  # Only the repository ownership answer is simulated.

    worker._lease_watch_wait = wait  # type: ignore[method-assign]
    worker.lease_still_owned = lost_lease  # type: ignore[method-assign]

    class Client:
        async def cancel(self, **fields: object) -> None:
            assert fields == {"job_id": job, "attempt": 1, "fence": 7}
            response = await asyncio.to_thread(
                harness._net_rpc, project, harness._request(job, fence, "cancel")
            )
            observations["cancel"] = response
            if response.get("cancelled") is not True:
                raise ExecutorCallError("cancel_not_running")

    snap = DownloadClaimSnapshot(
        job_id=uuid.UUID(job),
        media_job_id=uuid.uuid4(),
        fence=fence,
        attempt_count=1,
        format_option_id="fmt_1",
        provider_id="vk",
        canonical_provider_url="https://mock.invalid/blocking",
        media_id="mock",
        hostname="mock.invalid",
        path="/blocking",
        scheme="https",
        port=443,
        selected_format_snapshot={},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    rejected = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        remote = pool.submit(
            harness._net_rpc,
            project,
            harness._download_request(job, fence, "blocking", 1_000_000),
        )

        async def operation():  # type: ignore[no-untyped-def]
            payload = await asyncio.wrap_future(remote)
            return _process_result(payload)

        try:
            try:
                await worker._call_executor(
                    snap,
                    operation,
                    client=Client(),  # type: ignore[arg-type]
                )
            except _LeaseLostError:
                rejected = True
        finally:
            # Unconditional scoped cleanup; never used as cancellation proof.
            await asyncio.to_thread(
                harness._net_rpc, project, harness._request(job, fence, "cancel")
            )
            outcome = await asyncio.to_thread(remote.result, 20)
            before_tree = observations.get("before")
            after = await asyncio.to_thread(
                harness._fixture_snapshot,
                project,
                job,
                fence,
                expected=before_tree if isinstance(before_tree, dict) else None,
            )
            release = await asyncio.to_thread(
                harness._net_rpc, project, harness._request(job, fence, "release")
            )
    cancel = observations.get("cancel")
    before = observations.get("before")
    passed = (
        rejected
        and worker._lease_lost
        and isinstance(before, dict)
        and harness._tree_state(before, gone=False)
        and isinstance(cancel, dict)
        and cancel.get("cancelled") is True
        and outcome.get("cancelled") is True
        and harness._tree_state(after, gone=True)
        and release.get("code") == "released"
        and worker._watchdog_started == worker._watchdog_finished == 1
        and not worker._watchdog_tasks
    )
    return {
        "job_id": job,
        "fence": fence,
        "pass": passed,
        "lease_lost_rejected": rejected,
        "before": before,
        "cancel": cancel,
        "outcome": outcome,
        "after": after,
        "release": release,
        "repository_answer": "simulated_lost_lease",
        "database_ready_transaction": "NOT_RUN",
    }


def main() -> int:
    project = os.environ["SEC09_NATIVE_PROJECT"]
    if not project.startswith(harness.PROJECT_PREFIX):
        raise ValueError("unsafe native project")
    harness._ACTIVE_OVERLAY = os.environ["SEC09_NATIVE_OVERLAY"]
    path = Path(os.environ["SEC09_NATIVE_PROOF"])
    proof = asyncio.run(prove(project))
    harness.save(path, proof)
    return 0 if proof["pass"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
