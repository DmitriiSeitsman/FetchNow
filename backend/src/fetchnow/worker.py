"""Background worker process with graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal
from types import FrameType

from fetchnow.core.config import get_settings
from fetchnow.core.logging import configure_logging

logger = logging.getLogger(__name__)


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """Run the worker lifecycle loop until shutdown is requested."""
    settings = get_settings()
    configure_logging(settings.log_level)
    stop_event = stop_event or asyncio.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s; initiating graceful shutdown", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, None)
        except NotImplementedError:
            # Windows / restricted environments may not support signal handlers.
            signal.signal(sig, _handle_signal)

    logger.info(
        "Worker started env=%s poll_interval=%s concurrency=%s",
        settings.app_env,
        settings.worker_poll_interval_seconds,
        settings.worker_concurrency,
    )

    try:
        while not stop_event.is_set():
            logger.info("Worker heartbeat; idle (no jobs in this foundation PR)")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.worker_poll_interval_seconds,
                )
            except TimeoutError:
                continue
    finally:
        logger.info("Worker shut down cleanly")


def run() -> None:
    """CLI entrypoint for the worker process."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    run()
