"""Wiring the pieces together, and the loop that drives them.

Separated from ``__main__`` so a test — or an embedding process — can build the
whole service without going through argument parsing.

The loop is the interesting part. It is a plain ``while True`` with jitter and a
guard, and it is written on the assumption that it *will* be killed mid-sleep,
because that is what a container platform does on every deploy. Nothing is held
in memory between ticks that is not also in SQLite, so a restart loses at most
the sleep it was in the middle of.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from .apns import APNsClient
from .config import Config
from .nightscout import NightscoutClient
from .service import NotifierService
from .store import Store

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    # httpx logs a line per request at INFO, which at one tick every five
    # minutes across a few devices drowns everything that matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass
class Runtime:
    config: Config
    store: Store
    nightscout: NightscoutClient
    apns: APNsClient
    service: NotifierService

    async def aclose(self) -> None:
        await self.nightscout.aclose()
        await self.apns.aclose()


@contextlib.asynccontextmanager
async def build(config: Config) -> AsyncIterator[Runtime]:
    store = Store(config.database_path)
    nightscout = NightscoutClient(config.nightscout)
    apns = APNsClient(config.apns, host_override=config.apns_host_override)
    service = NotifierService(config, store, nightscout, apns)
    runtime = Runtime(config, store, nightscout, apns, service)
    try:
        yield runtime
    finally:
        await runtime.aclose()


async def run_loop(runtime: Runtime, stop: Optional[asyncio.Event] = None) -> None:
    """Tick forever, on an interval, until asked to stop.

    A failed tick does not break the loop and does not shorten the next
    interval: Nightscout being down for ten minutes should not turn into ten
    minutes of retries against a site that is already struggling. The failure is
    visible through ``/v1/health`` and through the absence of a heartbeat, which
    are the two places anyone is actually watching.
    """
    stop = stop or asyncio.Event()
    interval = max(30, runtime.config.poll_interval)
    jitter = max(0, runtime.config.poll_jitter)

    while not stop.is_set():
        try:
            await runtime.service.tick()
        except Exception:  # noqa: BLE001 - the loop outlives any one tick
            logger.exception("loop: tick raised")

        delay = interval + random.uniform(-jitter, jitter)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(5.0, delay))


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Stop on SIGTERM as well as SIGINT.

    SIGTERM is what a platform sends before it takes the container away, and a
    process that ignores it gets SIGKILLed a few seconds later — mid-write, if
    it is unlucky. WAL makes that survivable; handling the signal makes it not
    happen.
    """
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_number, stop.set)
