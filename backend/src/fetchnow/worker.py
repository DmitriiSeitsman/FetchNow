"""Background worker process with durable media-job orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
from types import FrameType

from fetchnow.core.config import get_settings
from fetchnow.core.logging import configure_logging
from fetchnow.jobs.worker_loop import MediaJobWorkerRunner

logger = logging.getLogger(__name__)


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """Run the worker lifecycle loop until shutdown is requested."""
    settings = get_settings()
    configure_logging(settings.log_level)
    stop_event = stop_event or asyncio.Event()

    runner = MediaJobWorkerRunner(settings, stop_event=stop_event)

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        runner.request_stop(signum)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, None)
        except NotImplementedError:
            signal.signal(sig, _handle_signal)

    await runner.run()


def run() -> None:
    """CLI entrypoint for the worker process."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    run()
