"""Entry point.

Four modes, because "how often should this run" is a deployment decision and
not something the code should have an opinion about:

* ``once``   — one tick, then exit. This is the cron-job shape, and the one the
               old ``script.py`` had. It still works, and ``script.py`` now
               forwards to it so an existing scheduled job keeps running.
* ``worker`` — the loop, no HTTP. Registration has to be served from somewhere
               else, so this is only useful alongside an ``api`` process
               sharing the volume.
* ``api``    — HTTP only, no loop. Pair with an external scheduler calling
               ``POST /v1/tick``.
* ``serve``  — both, in one process. The default, and what most deployments
               want: one container, one volume, no coordination.

``docs/DEPLOYMENT.md`` sets out when each is the right answer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config, ConfigError
from .runtime import build, configure_logging, install_signal_handlers, run_loop

logger = logging.getLogger("nsnotifier")


async def _once(config: Config) -> int:
    async with build(config) as runtime:
        result = await runtime.service.tick()
        # A non-zero exit is what makes a failed cron run *visible* to the
        # platform that scheduled it. The old script exited 0 whatever
        # happened, so a Nightscout outage and a quiet night were the same
        # event as far as any monitoring was concerned.
        return 0 if result.ok else 1


async def _worker(config: Config) -> int:
    async with build(config) as runtime:
        stop = asyncio.Event()
        install_signal_handlers(stop)
        await run_loop(runtime, stop)
    return 0


async def _serve(config: Config, with_loop: bool) -> int:
    import uvicorn

    from .api import create_app

    async with build(config) as runtime:
        app = create_app(config, runtime.store, runtime.service)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                log_level=config.log_level.lower(),
                # uvicorn installs its own handlers otherwise, and then the
                # loop task never sees the signal that is taking it away.
                access_log=False,
            )
        )

        stop = asyncio.Event()
        tasks = [asyncio.create_task(server.serve(), name="http")]
        if with_loop:
            tasks.append(asyncio.create_task(run_loop(runtime, stop), name="loop"))

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.set()
            server.should_exit = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nsnotifier", description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="serve",
        choices=["once", "worker", "api", "serve"],
    )
    arguments = parser.parse_args(argv)

    try:
        config = Config.from_environment()
    except ConfigError as error:
        # Deliberately before logging is configured: this has to be readable in
        # a platform's raw startup output, where a misconfigured deployment is
        # actually looked at.
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    logger.info("starting in %s mode", arguments.mode)

    if arguments.mode == "once":
        return asyncio.run(_once(config))
    if arguments.mode == "worker":
        return asyncio.run(_worker(config))
    if arguments.mode == "api":
        return asyncio.run(_serve(config, with_loop=False))
    return asyncio.run(_serve(config, with_loop=True))


if __name__ == "__main__":
    raise SystemExit(main())
